# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-script CLI tool ("Gradle Stack Toolkit") that statically parses `build.gradle` / `build.gradle.kts` files (without invoking Gradle) to extract the dependency/plugin stack of one or more Gradle projects into CSV, and flags projects that use an internal "chassi" (plugins `arch.springconfig` / `arch.buildconfig`). All logic lives in `src/springboot_dependabot/dependabot.py`.

## Running the tool

The package's console-script entry point is `check` (declared in `pyproject.toml` as `springboot_dependabot.dependabot:main`), so the preferred way to run it is:

```bash
uv run check <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...] [--keep-intermediate]
```

Running the module directly also works, without needing the package installed:

```bash
python3 src/springboot_dependabot/dependabot.py <diretorio_raiz> [-o saida.csv] [-x GROUP_ID ...] [--keep-intermediate]
```

`src/springboot_dependabot/__init__.py` is intentionally empty — it's just the package marker, no longer holding a placeholder `main()`.

Subcommands (default with no subcommand is `pipeline`):
- `extract <root> [-o output.csv] [-x GROUP_ID ...]` — static-parse only, emits `Projeto, Nome, Versao, Escopo`.
- `chassi <input.csv> [-o output.csv]` — reads an already-extracted CSV and adds the `Usa chassi` column.
- `pipeline <root> [-o output.csv] [-x GROUP_ID ...] [--keep-intermediate]` — runs extract then chassi in sequence (this is also what runs when no subcommand is given, e.g. `uv run check <root> ...`).

No external dependencies; requires Python 3.11+ for `tomllib` (version catalog parsing is skipped with a warning on older interpreters). This repo targets Python 3.13 (`.python-version`, `pyproject.toml`).

There is no test suite, linter, or build step configured beyond the `uv_build` packaging backend.

## Architecture

The single file `dependabot.py` is organized as three stages plus a CLI:

1. **Extractor** (`extract`, `parse_dependencies`, `parse_plugins`, `load_version_catalog`): discovers Gradle project roots under the given directory (`discover_projects` — each first-level subfolder containing `settings.gradle(.kts)` is treated as an independent project; falls back to treating the root itself as the single project if none are found), then regex-parses each `build.gradle`/`build.gradle.kts` for dependency declarations and plugin declarations. Dependency configs (`implementation`, `testImplementation`, etc.) are mapped to logical scopes (`build`, `development`, `runtime`, `test`) via `SCOPE_MAP`; everything else becomes `plugin`. Version Catalogs (`libs.versions.toml`) are loaded per-project and used to resolve `libs.xxx.yyy` aliases to real group:artifact:version. `project(":module")` internal dependencies are intentionally dropped (not external libraries). Results are deduplicated by `(project, name, version, scope)` and can be filtered by excluded `groupId` (exact match or namespace prefix; plugins are never excluded).
2. **Chassi detector** (`chassi`, `detect_chassi_projects`): reads an extractor-shaped CSV and marks every row belonging to a project where any plugin name contains `arch.springconfig` or `arch.buildconfig` as `Usa chassi = Sim`.
3. **Pipeline** (`pipeline`, `run_pipeline`): chains extract → chassi through a temp intermediate CSV, optionally preserved with `--keep-intermediate`.

The CLI (`build_parser`, `main`) supports omitting the subcommand entirely — if the first positional arg isn't `extract`/`chassi`/`pipeline`, `main()` implicitly prepends `pipeline` to preserve the behavior of an older standalone wrapper script.

Known static-parsing limitations (documented in both the file header and README): no resolution of complex externally-defined variables (e.g. `gradle.properties` logic), no `if/else` block evaluation, BOM/`platform()` captured as a plain dependency without propagating pinned versions.
