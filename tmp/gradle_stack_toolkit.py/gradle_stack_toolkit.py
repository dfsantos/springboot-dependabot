#!/usr/bin/env python3
"""
Gradle Stack Toolkit
----------------------
Script único que consolida:
    1. Extractor  — parsing estático de build.gradle/build.gradle.kts,
                     gera CSV: Projeto, Nome, Versao, Escopo.
    2. Chassi     — lê o CSV do extractor e marca projetos que usam
                     chassi (plugins arch.springconfig / arch.buildconfig),
                     acrescentando a coluna "Usa chassi" (Sim/Não).
    3. Pipeline   — executa as duas etapas acima em sequência.

Uso:
    # Comportamento padrão (equivalente ao antigo wrapper): extrai e detecta chassi
    python gradle_stack_toolkit.py <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...] [--keep-intermediate]

    # Rodar apenas a extração (equivalente ao antigo gradle_stack_extractor.py)
    python gradle_stack_toolkit.py extract <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...]

    # Rodar apenas a detecção de chassi sobre um CSV existente (equivalente ao antigo chassi_detector.py)
    python gradle_stack_toolkit.py chassi <entrada.csv> [-o saida.csv]

    # Pipeline explícito (idêntico ao padrão, mas nomeado)
    python gradle_stack_toolkit.py pipeline <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...] [--keep-intermediate]

Limitações conhecidas do parsing estático (etapa de extração):
    - Não resolve variáveis complexas definidas fora do arquivo (ex: em
      gradle.properties referenciadas por projeto.property(...) com lógica).
    - BOM / platform() são capturados como dependência, mas sem "explodir"
      as versões que eles fixam para as demais libs.
    - Blocos condicionais (if/else) não são interpretados; o script tenta
      capturar a declaração de qualquer forma, o que pode gerar falsos
      positivos em builds muito dinâmicos.
    - Version Catalogs (libs.versions.toml) são lidos separadamente e
      cruzados por alias quando o build script usa libs.xxx.yyy.
"""

import argparse
import csv
import re
import sys
import tempfile
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

try:
    import tomllib  # Python 3.11+
except ImportError:
    tomllib = None


# ==========================================================================
# ETAPA 1: EXTRACTOR
# ==========================================================================

# --------------------------------------------------------------------------
# Mapeamento de configurações Gradle -> escopo lógico solicitado
# --------------------------------------------------------------------------
SCOPE_MAP = {
    # build
    "implementation": "build",
    "api": "build",
    "compileOnly": "build",
    "compileOnlyApi": "build",
    "compile": "build",  # legado

    # development (geração de código / processamento em tempo de build,
    # mas conceitualmente "tooling" de desenvolvimento)
    "annotationProcessor": "development",
    "kapt": "development",
    "ksp": "development",

    # runtime
    "runtimeOnly": "runtime",
    "runtimeClasspath": "runtime",
    "runtime": "runtime",  # legado

    # test
    "testImplementation": "test",
    "testApi": "test",
    "testCompile": "test",  # legado
    "testCompileOnly": "test",
    "testRuntimeOnly": "test",
    "testAnnotationProcessor": "test",
    "androidTestImplementation": "test",
    "androidTestRuntimeOnly": "test",
    "debugImplementation": "test",
}

DEPENDENCY_CONFIGS = "|".join(sorted(SCOPE_MAP.keys(), key=len, reverse=True))

GRADLE_FILENAMES = ("build.gradle", "build.gradle.kts")
SETTINGS_FILENAMES = ("settings.gradle", "settings.gradle.kts")
VERSION_CATALOG_FILENAMES = ("libs.versions.toml",)


@dataclass
class Entry:
    project: str
    name: str
    version: str
    scope: str
    group_id: str = ""
    is_plugin: bool = False


# --------------------------------------------------------------------------
# Version Catalog (libs.versions.toml)
# --------------------------------------------------------------------------
def load_version_catalog(root: Path) -> dict:
    """Carrega libs.versions.toml (se existir) para resolver aliases tipo
    libs.jackson.databind -> versão real."""
    catalog = {"versions": {}, "libraries": {}, "plugins": {}}
    for toml_path in root.rglob("libs.versions.toml"):
        if tomllib is None:
            print(f"[aviso] tomllib indisponível; ignorando {toml_path}", file=sys.stderr)
            continue
        try:
            data = tomllib.loads(toml_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[aviso] falha ao ler {toml_path}: {e}", file=sys.stderr)
            continue

        versions = data.get("versions", {})
        catalog["versions"].update(versions)

        for alias, meta in data.get("libraries", {}).items():
            module = meta.get("module")
            group = meta.get("group")
            name = meta.get("name")
            ver_ref = None
            version = None
            if isinstance(meta.get("version"), dict):
                ver_ref = meta["version"].get("ref")
            elif isinstance(meta.get("version"), str):
                version = meta.get("version")
            elif isinstance(meta.get("version.ref"), str):
                ver_ref = meta.get("version.ref")

            if ver_ref:
                version = versions.get(ver_ref, f"ref:{ver_ref}")

            if module:
                full_name = module
            elif group and name:
                full_name = f"{group}:{name}"
            else:
                full_name = name or alias

            catalog["libraries"][alias.replace("-", ".").replace("_", ".")] = {
                "name": full_name,
                "version": version or "N/A",
            }
            # Também indexa pelo alias "cru" (como usado em libs.alias.dashed)
            catalog["libraries"][alias] = catalog["libraries"][alias.replace("-", ".").replace("_", ".")]

        for alias, meta in data.get("plugins", {}).items():
            ver_ref = None
            version = None
            if isinstance(meta.get("version"), dict):
                ver_ref = meta["version"].get("ref")
            elif isinstance(meta.get("version"), str):
                version = meta.get("version")
            if ver_ref:
                version = versions.get(ver_ref, f"ref:{ver_ref}")
            catalog["plugins"][alias] = {
                "name": meta.get("id", alias),
                "version": version or "N/A",
            }

    return catalog


def resolve_catalog_alias(token: str, catalog: dict) -> Optional[tuple]:
    """Resolve algo como 'libs.jackson.databind' ou 'libs.junit' para
    (nome, versao) usando o catálogo carregado."""
    if not token.startswith("libs."):
        return None
    alias_dotted = token[len("libs."):]
    lib = catalog["libraries"].get(alias_dotted)
    if lib:
        return lib["name"], lib["version"]
    # tenta variações com hífen
    alias_dashed = alias_dotted.replace(".", "-")
    lib = catalog["libraries"].get(alias_dashed)
    if lib:
        return lib["name"], lib["version"]
    return None


# --------------------------------------------------------------------------
# Descoberta de projetos
# --------------------------------------------------------------------------
def project_name_from_path(build_file: Path, root: Path) -> str:
    """Retorna sempre o nome do projeto raiz, independentemente de o
    build.gradle pertencer ao módulo raiz ou a um submódulo."""
    return root.name


def find_build_files(root: Path):
    for filename in GRADLE_FILENAMES:
        yield from root.rglob(filename)


def has_settings_file(path: Path) -> bool:
    return any((path / filename).is_file() for filename in SETTINGS_FILENAMES)


def discover_projects(root: Path):
    """Descobre as raízes de projetos Gradle dentro de 'root'.

    Cada subpasta de primeiro nível que contenha settings.gradle ou
    settings.gradle.kts é tratada como a raiz de um projeto independente
    (multi-módulo ou não). Se nenhuma subpasta atender esse critério —
    incluindo o caso em que 'root' já é, ele mesmo, a raiz de um único
    projeto — o próprio 'root' é retornado como projeto único, preservando
    o comportamento anterior do script (uso com um projeto por vez)."""
    if has_settings_file(root):
        return [root]

    projects = [
        child for child in sorted(root.iterdir())
        if child.is_dir() and has_settings_file(child)
    ]

    if projects:
        return projects

    # Nenhuma subpasta com settings.gradle encontrada: trata 'root' como
    # projeto único (fallback de compatibilidade, ex: build.gradle solto
    # sem settings.gradle, ou estrutura não reconhecida).
    return [root]


# --------------------------------------------------------------------------
# Parsing de dependências
# --------------------------------------------------------------------------
# Casos cobertos:
#   implementation("group:artifact:version")
#   implementation 'group:artifact:version'
#   implementation(libs.alias.dotted)
#   testImplementation project(":modulo")
#   implementation(group = "g", name = "n", version = "v")   (kts nomeado)

RE_STRING_DEP = re.compile(
    rf'\b({DEPENDENCY_CONFIGS})\s*[\(\s]\s*[\'"]([^\'"]+)[\'"]'
)

RE_CATALOG_DEP = re.compile(
    rf'\b({DEPENDENCY_CONFIGS})\s*[\(\s]\s*(libs(?:\.[A-Za-z0-9_]+)+)'
)

RE_PROJECT_DEP = re.compile(
    rf'\b({DEPENDENCY_CONFIGS})\s*[\(\s]\s*project\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)'
)

RE_NAMED_ARGS_DEP = re.compile(
    rf'\b({DEPENDENCY_CONFIGS})\s*\(?\s*group\s*[:=]\s*[\'"]([^\'"]+)[\'"]\s*,\s*name\s*[:=]\s*[\'"]([^\'"]+)[\'"]\s*(?:,\s*version\s*[:=]\s*[\'"]([^\'"]+)[\'"])?'
)

# Plugins: id("com.foo.bar") version "1.2.3"  |  id "com.foo.bar" version "1.2.3"
RE_PLUGIN_WITH_VERSION = re.compile(
    r'id\s*[\(\s]\s*[\'"]([^\'"]+)[\'"]\)?\s*version\s*[\'"]([^\'"]+)[\'"]'
)
RE_PLUGIN_NO_VERSION = re.compile(
    r'id\s*[\(\s]\s*[\'"]([^\'"]+)[\'"]\)?'
)
RE_APPLY_PLUGIN = re.compile(
    r'apply\s+plugin\s*:\s*[\'"]([^\'"]+)[\'"]'
)
RE_KOTLIN_SHORTHAND_PLUGIN = re.compile(
    r'\b(kotlin|java|application|war|groovy)\s*\(\s*[\'"]?([^\'")]*)[\'"]?\s*\)'
)


def strip_comments(text: str) -> str:
    text = re.sub(r'//.*', '', text)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return text


def artifact_id_only(group_artifact: str) -> str:
    """'group:artifact' ou 'group:artifact:version' -> 'artifact' (artifactId)."""
    parts = group_artifact.split(":")
    if len(parts) >= 2:
        return parts[1]
    return parts[0]


def parse_gav(gav: str):
    """'group:artifact:version' -> (artifactId, version, groupId). Aceita 2 ou 3 partes."""
    parts = gav.split(":")
    if len(parts) >= 3:
        name = parts[1]
        version = parts[2]
        group = parts[0]
    elif len(parts) == 2:
        name = parts[1]
        version = "N/A"
        group = parts[0]
    else:
        name = gav
        version = "N/A"
        group = ""
    return name, version, group


def parse_dependencies(text: str, project: str, catalog: dict):
    entries = []

    for m in RE_STRING_DEP.finditer(text):
        config, gav = m.group(1), m.group(2)
        name, version, group = parse_gav(gav)
        entries.append(Entry(project, name, version, SCOPE_MAP[config], group_id=group))

    for m in RE_CATALOG_DEP.finditer(text):
        config, alias = m.group(1), m.group(2)
        resolved = resolve_catalog_alias(alias, catalog)
        if resolved:
            full_name, version = resolved
            name = artifact_id_only(full_name)
            group = full_name.split(":")[0] if ":" in full_name else ""
        else:
            name, version, group = alias, "N/A (catálogo não resolvido)", ""
        entries.append(Entry(project, name, version, SCOPE_MAP[config], group_id=group))

    # Dependências internas entre módulos (project(":modulo")) são
    # intencionalmente ignoradas: não representam biblioteca externa.

    for m in RE_NAMED_ARGS_DEP.finditer(text):
        config, group, name_, version = m.group(1), m.group(2), m.group(3), m.group(4)
        entries.append(Entry(project, name_, version or "N/A", SCOPE_MAP[config], group_id=group))

    return entries


def parse_plugins(text: str, project: str, catalog: dict):
    entries = []
    handled_ids_at_pos = set()

    # Indexa o catálogo de plugins também por id (não só por alias), já que
    # o build script referencia plugins por id ("org.springframework.boot"),
    # não pelo alias do catálogo ("spring-boot").
    plugins_by_id = {meta["name"]: meta for meta in catalog["plugins"].values()}

    for m in RE_PLUGIN_WITH_VERSION.finditer(text):
        plugin_id, version = m.group(1), m.group(2)
        entries.append(Entry(project, plugin_id, version, "plugin", is_plugin=True))
        handled_ids_at_pos.add((plugin_id, m.start()))

    for m in RE_PLUGIN_NO_VERSION.finditer(text):
        plugin_id = m.group(1)
        if any(plugin_id == pid and abs(m.start() - pos) < 5 for pid, pos in handled_ids_at_pos):
            continue
        plugin_meta = catalog["plugins"].get(plugin_id) or plugins_by_id.get(plugin_id)
        version = plugin_meta["version"] if plugin_meta else "N/A"
        entries.append(Entry(project, plugin_id, version, "plugin", is_plugin=True))

    for m in RE_APPLY_PLUGIN.finditer(text):
        plugin_id = m.group(1)
        entries.append(Entry(project, plugin_id, "N/A", "plugin", is_plugin=True))

    for m in RE_KOTLIN_SHORTHAND_PLUGIN.finditer(text):
        kind, arg = m.group(1), m.group(2)
        label = f"{kind}({arg})" if arg else kind
        entries.append(Entry(project, label, "N/A", "plugin", is_plugin=True))

    return entries


# --------------------------------------------------------------------------
# Execução da extração
# --------------------------------------------------------------------------
def deduplicate_entries(entries: list) -> list:
    """Remove entradas repetidas quando a mesma dependência/plugin é
    declarada em mais de um módulo com o mesmo nome, versão e escopo.
    Preserva a ordem de primeira ocorrência. Se a mesma dependência
    aparecer com versões diferentes entre módulos, ambas as linhas são
    mantidas, pois isso representa um conflito real, não uma duplicata."""
    seen = set()
    unique = []
    for e in entries:
        key = (e.project, e.name, e.version, e.scope)
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)
    return unique


def is_excluded(entry: "Entry", excluded_groups: list) -> bool:
    """Verifica se a entrada deve ser descartada por pertencer a um dos
    groupIds excluídos. Aplica-se apenas a dependências (compara group_id);
    plugins são sempre mantidos, independentemente do groupId, pois
    representam configuração de build e não uma biblioteca consumida."""
    if not excluded_groups or entry.is_plugin:
        return False
    if not entry.group_id:
        return False
    for excluded in excluded_groups:
        if entry.group_id == excluded or entry.group_id.startswith(excluded + "."):
            return True
    return False


def extract(root: Path, excluded_groups: list = None):
    excluded_groups = excluded_groups or []
    all_entries = []

    projects = discover_projects(root)
    if len(projects) > 1:
        print(f"[info] {len(projects)} projetos detectados em '{root}':", file=sys.stderr)
        for p in projects:
            print(f"  - {p.name}", file=sys.stderr)

    for project_root in projects:
        # Catálogo de versões é carregado por projeto: cada um pode ter seu
        # próprio libs.versions.toml, e aliases não devem vazar entre projetos.
        catalog = load_version_catalog(project_root)

        for build_file in find_build_files(project_root):
            project = project_name_from_path(build_file, project_root)
            try:
                raw = build_file.read_text(encoding="utf-8")
            except Exception as e:
                print(f"[aviso] não foi possível ler {build_file}: {e}", file=sys.stderr)
                continue

            text = strip_comments(raw)
            all_entries.extend(parse_dependencies(text, project, catalog))
            all_entries.extend(parse_plugins(text, project, catalog))

    if excluded_groups:
        all_entries = [e for e in all_entries if not is_excluded(e, excluded_groups)]

    all_entries = deduplicate_entries(all_entries)

    return all_entries


def write_extract_csv(entries, output_path: Path):
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["Projeto", "Nome", "Versao", "Escopo"])
        for e in sorted(entries, key=lambda x: (x.project, x.scope, x.name)):
            writer.writerow([e.project, e.name, e.version, e.scope])


def run_extract(root: Path, output_path: Path, excluded_groups: list = None):
    entries = extract(root, excluded_groups=excluded_groups)
    if not entries:
        print("[aviso] Nenhuma dependência/plugin encontrado. Verifique o caminho informado.", file=sys.stderr)
    write_extract_csv(entries, output_path)
    print(f"OK: {len(entries)} itens extraídos -> {output_path}")
    return entries


# ==========================================================================
# ETAPA 2: CHASSI DETECTOR
# ==========================================================================

CHASSI_PLUGINS = {"arch.springconfig", "arch.buildconfig"}


def read_chassi_input_rows(input_path: Path):
    with input_path.open("r", newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    required = {"Projeto", "Nome", "Versao", "Escopo"}
    if not rows or not required.issubset(reader.fieldnames or []):
        print(
            f"Erro: '{input_path}' não parece ser um CSV no formato esperado "
            f"(colunas: {', '.join(sorted(required))}).",
            file=sys.stderr,
        )
        sys.exit(1)
    return rows, reader.fieldnames


def detect_chassi_projects(rows):
    """Retorna o conjunto de projetos que possuem algum plugin cujo
    identificador contenha uma das substrings de chassi (ex:
    'com.empresa.arch.buildconfig.gradle' também conta)."""
    chassi_projects = set()
    for row in rows:
        nome = row["Nome"]
        if any(marker in nome for marker in CHASSI_PLUGINS):
            chassi_projects.add(row["Projeto"])
    return chassi_projects


def write_chassi_output(rows, fieldnames, chassi_projects, output_path: Path):
    out_fieldnames = list(fieldnames) + ["Usa chassi"]
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        for row in rows:
            row_out = dict(row)
            row_out["Usa chassi"] = "Sim" if row["Projeto"] in chassi_projects else "Não"
            writer.writerow(row_out)


def run_chassi(input_path: Path, output_path: Path):
    rows, fieldnames = read_chassi_input_rows(input_path)
    chassi_projects = detect_chassi_projects(rows)
    write_chassi_output(rows, fieldnames, chassi_projects, output_path)

    print(f"OK: {len(rows)} linhas processadas -> {output_path}")
    if chassi_projects:
        print(f"Projetos com chassi detectado ({len(chassi_projects)}):")
        for p in sorted(chassi_projects):
            print(f"  - {p}")
    else:
        print("Nenhum projeto com chassi detectado.")


# ==========================================================================
# ETAPA 3: PIPELINE (extract -> chassi)
# ==========================================================================

def run_pipeline(root: Path, output_path: Path, excluded_groups: list = None, keep_intermediate: bool = False):
    with tempfile.TemporaryDirectory() as tmp_dir:
        intermediate_path = Path(tmp_dir) / "gradle_stack_intermediate.csv"

        print(f"[1/2] Extraindo stack de '{root}'...")
        run_extract(root, intermediate_path, excluded_groups=excluded_groups)

        if not intermediate_path.exists():
            print("Erro: extração não gerou o CSV intermediário esperado.", file=sys.stderr)
            sys.exit(1)

        print("[2/2] Detectando uso de chassi...")
        run_chassi(intermediate_path, output_path)

        if keep_intermediate:
            kept_path = output_path.parent / f"{output_path.stem}_intermediate.csv"
            kept_path.write_bytes(intermediate_path.read_bytes())
            print(f"CSV intermediário preservado em: {kept_path}")

    print(f"Pipeline concluído -> {output_path}")


# ==========================================================================
# CLI
# ==========================================================================

def add_exclude_group_arg(parser):
    parser.add_argument(
        "-x", "--exclude-group",
        action="append",
        default=[],
        metavar="GROUP_ID",
        help="GroupId a ser ignorado na extração de dependências (plugins não "
             "são afetados por este filtro). Repetível: -x org.springframework "
             "-x com.empresa.interno. Aplica correspondência exata ou por "
             "prefixo de namespace (ex: 'org.springframework' também exclui "
             "'org.springframework.security')."
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Gradle Stack Toolkit: extrai stack de projetos Gradle e detecta uso de chassi."
    )
    subparsers = parser.add_subparsers(dest="command")

    # Subcomando: extract
    p_extract = subparsers.add_parser("extract", help="Apenas extrai a stack (dependências/plugins) do projeto Gradle.")
    p_extract.add_argument("root", type=str, help="Diretório raiz do projeto Gradle (multi-módulo ou não).")
    p_extract.add_argument("-o", "--output", type=str, default="gradle_stack.csv", help="Caminho do CSV de saída.")
    add_exclude_group_arg(p_extract)

    # Subcomando: chassi
    p_chassi = subparsers.add_parser("chassi", help="Apenas detecta uso de chassi a partir de um CSV já extraído.")
    p_chassi.add_argument("input", type=str, help="Caminho do CSV de entrada (gerado pela etapa de extração).")
    p_chassi.add_argument("-o", "--output", type=str, default="gradle_stack_chassi.csv", help="Caminho do CSV de saída.")

    # Subcomando: pipeline (explícito)
    p_pipeline = subparsers.add_parser("pipeline", help="Executa extração seguida de detecção de chassi (equivalente ao comportamento padrão).")
    p_pipeline.add_argument("root", type=str, help="Diretório raiz do projeto Gradle.")
    p_pipeline.add_argument("-o", "--output", type=str, default="gradle_stack_chassi.csv", help="Caminho do CSV final de saída.")
    add_exclude_group_arg(p_pipeline)
    p_pipeline.add_argument("--keep-intermediate", action="store_true", help="Preserva o CSV intermediário (saída da extração) para inspeção.")

    return parser


def main():
    # Suporte ao modo "padrão" (sem subcomando): equivalente a `pipeline`.
    # Detecta se o primeiro argumento posicional é um subcomando conhecido;
    # caso contrário, insere "pipeline" implicitamente para manter o
    # comportamento do antigo script wrapper (gradle_stack_pipeline.py).
    known_commands = {"extract", "chassi", "pipeline"}
    argv = sys.argv[1:]
    if argv and argv[0] not in known_commands and not argv[0].startswith("-"):
        argv = ["pipeline"] + argv
    elif not argv:
        argv = ["--help"]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "extract":
        root = Path(args.root).resolve()
        if not root.exists():
            print(f"Erro: diretório '{root}' não existe.", file=sys.stderr)
            sys.exit(1)
        output_path = Path(args.output).resolve()
        run_extract(root, output_path, excluded_groups=args.exclude_group)

    elif args.command == "chassi":
        input_path = Path(args.input).resolve()
        if not input_path.exists():
            print(f"Erro: arquivo '{input_path}' não existe.", file=sys.stderr)
            sys.exit(1)
        output_path = Path(args.output).resolve()
        run_chassi(input_path, output_path)

    elif args.command == "pipeline":
        root = Path(args.root).resolve()
        if not root.exists():
            print(f"Erro: diretório '{root}' não existe.", file=sys.stderr)
            sys.exit(1)
        output_path = Path(args.output).resolve()
        run_pipeline(root, output_path, excluded_groups=args.exclude_group, keep_intermediate=args.keep_intermediate)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()