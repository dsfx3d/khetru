# khetru

Free, open-source farming companion for Indian smallholders. See [STRATEGY.md](STRATEGY.md).

## Layout

Polyglot monorepo: TypeScript via pnpm workspaces, Python via uv workspaces.

- `apps/` — TypeScript apps (website)
- `packages/` — TypeScript libraries
- `py/` — Python packages (uv workspace members)

## Setup

Requires Node 24+, pnpm 10, Python 3.13+, [uv](https://docs.astral.sh/uv/).

```sh
pnpm install
uv sync
pnpm test   # TS package tests, then pytest across py/*
```

## Adding a package

- **TypeScript:** create `apps/<name>/` or `packages/<name>/` with a `package.json`.
- **Python:** create `py/<name>/` with a `pyproject.toml` (`uv init --package py/<name>`); tests go in `py/<name>/tests/`.
