# Gradle Stack Toolkit

Extrai a stack de dependências e plugins de projetos Gradle (parsing estático, sem executar o Gradle) e identifica projetos que usam algum Plugin Legado.

## O que faz

- Clona automaticamente, antes da extração, os repositórios que ainda não existem dentro da pasta raiz — vindos de um arquivo de registro (`repos.txt`, um path por linha) e/ou da flag `--repo`. Aceita URLs SSH e HTTPS; falha ao clonar não interrompe o fluxo.
- Varre `build.gradle` / `build.gradle.kts` recursivamente (suporta multi-módulo).
- Gera CSV com: `Projeto, Nome, Versao, Escopo` (escopos: `build`, `development`, `runtime`, `test`, `plugin`).
- Resolve Version Catalogs (`libs.versions.toml`).
- Marca projetos que usam algum **Plugin Legado** na coluna `Usa Plugin Legado` (Sim/Não). A lista de identificadores de plugin pesquisados é parametrizável via `--legacy-plugin` ou variável de ambiente `LEGACY_PLUGINS`; se nenhum dos dois for informado, usa o padrão (`arch.springconfig`, `arch.buildconfig`).
- Permite excluir dependências por groupId (plugins nunca são excluídos).
- Deduplica entradas repetidas entre módulos.

## Pré-requisitos

- Python 3.11+
- Nenhuma dependência externa.

## Como usar

```bash
uv run check projetos -o cce.csv -x com.acme -x br.com.acme
```

Alternativamente, sem o `uv`, é possível rodar o script diretamente:

```bash
python3 src/springboot_dependabot/dependabot.py projetos -o cce.csv -x com.acme -x br.com.acme
```

onde:

- **projetos**: é a pasta onde todos os repositórios estão
- **cce.csv**: nome do arquivo de saída com os dados coletados
- **-x com.acme** e **-x br.com.acme**: exclusão de dependências que tenham este groupId (isso evita de detectar dependências internas como bibliotecas)


### Opções úteis

- `-x GROUP_ID` (repetível) — ignora dependências desse groupId (ex: `-x org.springframework`). Só afeta dependências, não plugins.
- `--legacy-plugin PLUGIN_ID` (repetível) — identificador (ou substring) de plugin considerado "Plugin Legado" para a coluna `Usa Plugin Legado`. Também pode ser definido via variável de ambiente `LEGACY_PLUGINS` (lista separada por vírgula). Precedência: `--legacy-plugin` > `LEGACY_PLUGINS` > padrão (`arch.springconfig`, `arch.buildconfig`).

```bash
# Via linha de comando
uv run check projetos -o cce.csv --legacy-plugin com.empresa.arch.custom

# Via variável de ambiente
LEGACY_PLUGINS="com.empresa.arch.custom,com.empresa.arch.outro" uv run check projetos -o cce.csv
```

- `--repo URL` (repetível) — URL de repositório git (SSH ou HTTPS) a clonar para dentro da pasta raiz antes da extração, caso ainda não exista localmente. URLs novas são automaticamente acrescentadas ao arquivo de registro (`--repos-file`, padrão `repos.txt`, dentro da própria pasta raiz) para serem reaproveitadas nas próximas execuções. Falha ao clonar não interrompe o fluxo.
- `--repos-file NOME` — nome do arquivo de registro de repositórios (um path por linha). Padrão: `repos.txt`.

```bash
# Primeira execução: clona os repositórios informados para dentro de "projetos/" e registra em projetos/repos.txt
uv run check projetos -o cce.csv --repo git@github.com:empresa/repo-a.git --repo https://github.com/empresa/repo-b.git

# Execuções seguintes: basta editar projetos/repos.txt (um path por linha) ou rodar sem --repo
uv run check projetos -o cce.csv
```

## Limitações (parsing estático)

- Não resolve variáveis complexas definidas fora do arquivo (ex: `gradle.properties` com lógica).
- BOM/`platform()` aparecem como uma dependência, sem propagar a versão fixada para as demais libs.
- Blocos condicionais (`if/else`) não são interpretados.
- Dependências internas entre módulos (`project(":modulo")`) são ignoradas — não representam biblioteca externa.
