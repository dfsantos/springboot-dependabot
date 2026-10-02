#!/usr/bin/env python3
"""
Gradle Stack Toolkit
----------------------
Script único que consolida:
    0. Clonagem      — antes de extrair, clona (se ainda não existirem em
                        disco) os repositórios listados em repos.txt (dentro
                        do diretório raiz) e/ou informados via --repo.
                        Aceita URLs SSH e HTTPS; falha ao clonar não
                        interrompe o fluxo.
    1. Extractor     — parsing estático de build.gradle/build.gradle.kts,
                        gera CSV: Projeto, Nome, Versao, Escopo.
    2. Plugin Legado  — lê o CSV do extractor e marca projetos que usam
                        algum Plugin Legado (identificadores configuráveis
                        via --legacy-plugin ou variável de ambiente
                        LEGACY_PLUGINS; padrão: arch.springconfig,
                        arch.buildconfig), acrescentando a coluna
                        "Usa Plugin Legado" (Sim/Não).
    3. Pipeline      — executa as etapas acima em sequência.

Uso:
    # Comportamento padrão (equivalente ao antigo wrapper): clona, extrai e detecta uso de Plugin Legado
    python gradle_stack_toolkit.py <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...] [--legacy-plugin PLUGIN_ID ...] [--repo URL ...] [--repos-file NOME] [--keep-intermediate]

    # Rodar apenas a extração (equivalente ao antigo gradle_stack_extractor.py)
    python gradle_stack_toolkit.py extract <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...] [--repo URL ...] [--repos-file NOME]

    # Rodar apenas a detecção de Plugin Legado sobre um CSV existente
    python gradle_stack_toolkit.py legacy-plugin <entrada.csv> [-o saida.csv] [--legacy-plugin PLUGIN_ID ...]

    # Pipeline explícito (idêntico ao padrão, mas nomeado)
    python gradle_stack_toolkit.py pipeline <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...] [--legacy-plugin PLUGIN_ID ...] [--repo URL ...] [--repos-file NOME] [--keep-intermediate]

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
import os
import re
import subprocess
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
# ETAPA 0: CLONAGEM DE REPOSITÓRIOS
# ==========================================================================

DEFAULT_REPOS_FILENAME = "repos.txt"


def read_repo_paths_file(path: Path) -> list:
    """Lê um path (URL de repositório) por linha; ignora linhas em branco e
    comentários (iniciados com #)."""
    if not path.is_file():
        return []
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def append_new_repo_paths(path: Path, new_urls: list) -> None:
    """Acrescenta URLs ainda não presentes no arquivo (cria arquivo/pastas se necessário)."""
    if not new_urls:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for url in new_urls:
            f.write(url + "\n")


def resolve_repo_urls(root: Path, cli_repos: list, repos_filename: str) -> list:
    """Combina o arquivo de registro (repos.txt) com novas URLs vindas de
    --repo. URLs novas via CLI são persistidas no arquivo (registro
    cumulativo), para serem reaproveitadas nas próximas execuções."""
    repos_file = root / repos_filename
    file_urls = read_repo_paths_file(repos_file)
    new_cli_urls = [u for u in cli_repos if u not in file_urls]
    append_new_repo_paths(repos_file, new_cli_urls)

    all_urls = []
    seen = set()
    for url in file_urls + new_cli_urls:
        if url not in seen:
            seen.add(url)
            all_urls.append(url)
    return all_urls


def repo_dir_name(url: str) -> str:
    """Deriva o nome do diretório local a partir de uma URL de repositório
    SSH (git@host:org/repo.git) ou HTTPS (https://host/org/repo.git)."""
    name = url.rstrip("/").rsplit("/", 1)[-1]
    name = name.rsplit(":", 1)[-1]  # cobre o raro caso sem "/" (git@host:repo.git)
    if name.endswith(".git"):
        name = name[:-4]
    return name


def clone_repo(url: str, dest: Path) -> bool:
    """Tenta clonar 'url' em 'dest'. Nunca levanta exceção nem interrompe o
    fluxo do script: qualquer falha (rede, credencial, URL inválida, git
    ausente, timeout) é reportada em stderr e tratada como não-fatal."""
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10",
    }
    try:
        result = subprocess.run(
            ["git", "clone", url, str(dest)],
            env=env, capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"[aviso] falha ao clonar '{url}': {e}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"[aviso] falha ao clonar '{url}': {result.stderr.strip()}", file=sys.stderr)
        return False
    print(f"[info] clonado '{url}' -> {dest}", file=sys.stderr)
    return True


def clone_missing_repos(root: Path, repo_urls: list) -> None:
    """Clona em 'root' cada URL de 'repo_urls' cujo diretório de destino
    ainda não existe. Repositórios já clonados são silenciosamente pulados."""
    for url in repo_urls:
        dest = root / repo_dir_name(url)
        if dest.exists():
            continue
        clone_repo(url, dest)


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


def run_extract(root: Path, output_path: Path, excluded_groups: list = None, cli_repos: list = None, repos_filename: str = DEFAULT_REPOS_FILENAME):
    root.mkdir(parents=True, exist_ok=True)
    repo_urls = resolve_repo_urls(root, cli_repos or [], repos_filename)
    if repo_urls:
        clone_missing_repos(root, repo_urls)

    entries = extract(root, excluded_groups=excluded_groups)
    if not entries:
        print("[aviso] Nenhuma dependência/plugin encontrado. Verifique o caminho informado.", file=sys.stderr)
    write_extract_csv(entries, output_path)
    print(f"OK: {len(entries)} itens extraídos -> {output_path}")
    return entries


# ==========================================================================
# ETAPA 2: DETECTOR DE PLUGIN LEGADO
# ==========================================================================

DEFAULT_LEGACY_PLUGIN_IDS = ("arch.springconfig", "arch.buildconfig")
LEGACY_PLUGIN_ENV_VAR = "LEGACY_PLUGINS"


def resolve_legacy_plugin_ids(cli_values: list) -> list:
    """Resolve a lista de identificadores de Plugin Legado a pesquisar.

    Precedência: --legacy-plugin (CLI) > LEGACY_PLUGINS (env var) > padrão."""
    if cli_values:
        return cli_values
    env_value = os.environ.get(LEGACY_PLUGIN_ENV_VAR)
    if env_value:
        return [v.strip() for v in env_value.split(",") if v.strip()]
    return list(DEFAULT_LEGACY_PLUGIN_IDS)


def read_legacy_plugin_input_rows(input_path: Path):
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


def detect_legacy_plugin_projects(rows, legacy_plugin_ids):
    """Retorna o conjunto de projetos que possuem algum plugin cujo
    identificador contenha uma das substrings de Plugin Legado informadas
    (ex: 'com.empresa.arch.buildconfig.gradle' também conta)."""
    legacy_plugin_projects = set()
    for row in rows:
        nome = row["Nome"]
        if any(marker in nome for marker in legacy_plugin_ids):
            legacy_plugin_projects.add(row["Projeto"])
    return legacy_plugin_projects


def write_legacy_plugin_output(rows, fieldnames, legacy_plugin_projects, output_path: Path):
    out_fieldnames = list(fieldnames) + ["Usa Plugin Legado"]
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        for row in rows:
            row_out = dict(row)
            row_out["Usa Plugin Legado"] = "Sim" if row["Projeto"] in legacy_plugin_projects else "Não"
            writer.writerow(row_out)


def run_legacy_plugin(input_path: Path, output_path: Path, legacy_plugin_ids: list = None):
    resolved_ids = resolve_legacy_plugin_ids(legacy_plugin_ids)
    rows, fieldnames = read_legacy_plugin_input_rows(input_path)
    legacy_plugin_projects = detect_legacy_plugin_projects(rows, resolved_ids)
    write_legacy_plugin_output(rows, fieldnames, legacy_plugin_projects, output_path)

    print(f"OK: {len(rows)} linhas processadas -> {output_path}")
    if legacy_plugin_projects:
        print(f"Projetos com Plugin Legado detectado ({len(legacy_plugin_projects)}):")
        for p in sorted(legacy_plugin_projects):
            print(f"  - {p}")
    else:
        print("Nenhum projeto com Plugin Legado detectado.")


# ==========================================================================
# ETAPA 3: PIPELINE (extract -> legacy-plugin)
# ==========================================================================

def run_pipeline(root: Path, output_path: Path, excluded_groups: list = None, keep_intermediate: bool = False, legacy_plugin_ids: list = None, cli_repos: list = None, repos_filename: str = DEFAULT_REPOS_FILENAME):
    with tempfile.TemporaryDirectory() as tmp_dir:
        intermediate_path = Path(tmp_dir) / "gradle_stack_intermediate.csv"

        print(f"[1/2] Extraindo stack de '{root}'...")
        run_extract(root, intermediate_path, excluded_groups=excluded_groups, cli_repos=cli_repos, repos_filename=repos_filename)

        if not intermediate_path.exists():
            print("Erro: extração não gerou o CSV intermediário esperado.", file=sys.stderr)
            sys.exit(1)

        print("[2/2] Detectando uso de Plugin Legado...")
        run_legacy_plugin(intermediate_path, output_path, legacy_plugin_ids=legacy_plugin_ids)

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


def add_legacy_plugin_arg(parser):
    parser.add_argument(
        "--legacy-plugin",
        action="append",
        default=[],
        metavar="PLUGIN_ID",
        help="Identificador (ou substring) de plugin considerado 'Plugin "
             "Legado' para a coluna 'Usa Plugin Legado'. Repetível: "
             "--legacy-plugin arch.springconfig --legacy-plugin "
             "arch.buildconfig. Se omitido, usa a variável de ambiente "
             f"{LEGACY_PLUGIN_ENV_VAR} (lista separada por vírgula) e, na "
             "ausência desta, o padrão "
             f"({', '.join(DEFAULT_LEGACY_PLUGIN_IDS)})."
    )


def add_repo_args(parser):
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        metavar="URL",
        help="URL de repositório git (SSH ou HTTPS) a clonar para dentro do "
             "diretório raiz antes da extração, caso ainda não exista "
             "localmente. Repetível: --repo git@host:org/foo.git --repo "
             "https://host/org/bar.git. URLs novas são automaticamente "
             "acrescentadas ao arquivo de registro (ver --repos-file) para "
             "serem reaproveitadas nas próximas execuções. Falha ao clonar "
             "não interrompe o fluxo."
    )
    parser.add_argument(
        "--repos-file",
        type=str,
        default=DEFAULT_REPOS_FILENAME,
        metavar="NOME",
        help="Nome do arquivo de registro de repositórios (um path por "
             f"linha), lido/gravado dentro do diretório raiz. Padrão: "
             f"{DEFAULT_REPOS_FILENAME}."
    )


def build_parser():
    parser = argparse.ArgumentParser(
        description="Gradle Stack Toolkit: extrai stack de projetos Gradle e detecta uso de Plugin Legado."
    )
    subparsers = parser.add_subparsers(dest="command")

    # Subcomando: extract
    p_extract = subparsers.add_parser("extract", help="Apenas extrai a stack (dependências/plugins) do projeto Gradle.")
    p_extract.add_argument("root", type=str, help="Diretório raiz do projeto Gradle (multi-módulo ou não).")
    p_extract.add_argument("-o", "--output", type=str, default="gradle_stack.csv", help="Caminho do CSV de saída.")
    add_exclude_group_arg(p_extract)
    add_repo_args(p_extract)

    # Subcomando: legacy-plugin
    p_legacy_plugin = subparsers.add_parser("legacy-plugin", help="Apenas detecta uso de Plugin Legado a partir de um CSV já extraído.")
    p_legacy_plugin.add_argument("input", type=str, help="Caminho do CSV de entrada (gerado pela etapa de extração).")
    p_legacy_plugin.add_argument("-o", "--output", type=str, default="gradle_stack_legacy_plugin.csv", help="Caminho do CSV de saída.")
    add_legacy_plugin_arg(p_legacy_plugin)

    # Subcomando: pipeline (explícito)
    p_pipeline = subparsers.add_parser("pipeline", help="Executa extração seguida de detecção de Plugin Legado (equivalente ao comportamento padrão).")
    p_pipeline.add_argument("root", type=str, help="Diretório raiz do projeto Gradle.")
    p_pipeline.add_argument("-o", "--output", type=str, default="gradle_stack_legacy_plugin.csv", help="Caminho do CSV final de saída.")
    add_exclude_group_arg(p_pipeline)
    add_legacy_plugin_arg(p_pipeline)
    add_repo_args(p_pipeline)
    p_pipeline.add_argument("--keep-intermediate", action="store_true", help="Preserva o CSV intermediário (saída da extração) para inspeção.")

    return parser


def main():
    # Suporte ao modo "padrão" (sem subcomando): equivalente a `pipeline`.
    # Detecta se o primeiro argumento posicional é um subcomando conhecido;
    # caso contrário, insere "pipeline" implicitamente para manter o
    # comportamento do antigo script wrapper (gradle_stack_pipeline.py).
    known_commands = {"extract", "legacy-plugin", "pipeline"}
    argv = sys.argv[1:]
    if argv and argv[0] not in known_commands and not argv[0].startswith("-"):
        argv = ["pipeline"] + argv
    elif not argv:
        argv = ["--help"]

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "extract":
        root = Path(args.root).resolve()
        output_path = Path(args.output).resolve()
        run_extract(root, output_path, excluded_groups=args.exclude_group, cli_repos=args.repo, repos_filename=args.repos_file)

    elif args.command == "legacy-plugin":
        input_path = Path(args.input).resolve()
        if not input_path.exists():
            print(f"Erro: arquivo '{input_path}' não existe.", file=sys.stderr)
            sys.exit(1)
        output_path = Path(args.output).resolve()
        run_legacy_plugin(input_path, output_path, legacy_plugin_ids=args.legacy_plugin)

    elif args.command == "pipeline":
        root = Path(args.root).resolve()
        output_path = Path(args.output).resolve()
        run_pipeline(root, output_path, excluded_groups=args.exclude_group, keep_intermediate=args.keep_intermediate, legacy_plugin_ids=args.legacy_plugin, cli_repos=args.repo, repos_filename=args.repos_file)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()