# khetru

Free, open-source farming companion for Indian smallholders. See [STRATEGY.md](STRATEGY.md).

## Layout

Polyglot monorepo. pnpm workspaces drive every package; Python packages are also uv workspace members.

- `apps/` — deployable things (website, jobs)
- `packages/` — shared libraries

## Setup

Requires Node 24+, pnpm 10, Python 3.13+, [uv](https://docs.astral.sh/uv/).

```sh
pnpm install
uv sync
pnpm test   # runs `test` in every package that defines it
```

## Adding a package

- **TypeScript:** create `apps/<name>/` or `packages/<name>/` with a `package.json`.
- **Python:** create the directory with a `pyproject.toml`, add its path to `members` in the root `pyproject.toml`, and add a `package.json` whose scripts call `uv run …` so `pnpm -r` reaches it.
