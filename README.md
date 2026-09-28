# Gradle Stack Toolkit

Extrai a stack de dependências e plugins de projetos Gradle (parsing estático, sem executar o Gradle) e identifica projetos que usam chassi.

## O que faz

- Varre `build.gradle` / `build.gradle.kts` recursivamente (suporta multi-módulo).
- Gera CSV com: `Projeto, Nome, Versao, Escopo` (escopos: `build`, `development`, `runtime`, `test`, `plugin`).
- Resolve Version Catalogs (`libs.versions.toml`).
- Marca projetos que usam chassi (plugins com `arch.springconfig` ou `arch.buildconfig` no identificador) na coluna `Usa chassi` (Sim/Não).
- Permite excluir dependências por groupId (plugins nunca são excluídos).
- Deduplica entradas repetidas entre módulos.

## Pré-requisitos

- Python 3.11+
- Nenhuma dependência externa.

## Como usar

```bash
python3 gradle_stack_toolkit.py projetos -o cce.csv -x com.acme -x br.com.acme
```

onde:

- **contacorrente**: é a pasta onde todos os repositórios estão
- **cce.csv**: nome do arquivo de saída com os dados coletados
- **-x br.com.unicred** e **-x br.com.unicred**: exlusão de dependências que tenham este groupId (isso evita de detectar dependências internas como bibliotecas)


### Opções úteis

- `-x GROUP_ID` (repetível) — ignora dependências desse groupId (ex: `-x org.springframework`). Só afeta dependências, não plugins.

## Limitações (parsing estático)

- Não resolve variáveis complexas definidas fora do arquivo (ex: `gradle.properties` com lógica).
- BOM/`platform()` aparecem como uma dependência, sem propagar a versão fixada para as demais libs.
- Blocos condicionais (`if/else`) não são interpretados.
- Dependências internas entre módulos (`project(":modulo")`) são ignoradas — não representam biblioteca externa.
