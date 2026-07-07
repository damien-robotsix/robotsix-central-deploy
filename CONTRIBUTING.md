# Contributing

Thanks for contributing to `robotsix-central-deploy`! This document covers
everything you need to get started.

## Before you begin

This repository follows the shared
[robotsix-standards](https://github.com/damien-robotsix/robotsix-standards).
Read that first for baseline conventions around tooling, CI, packaging, and
code style.

For **agent-oriented conventions** specific to this repo — the state machine,
component model, endpoints, and code gotchas — see [`AGENT.md`](AGENT.md).
Every automated contributor (human or otherwise) should read it before making
changes.

## Development setup

```bash
git clone https://github.com/damien-robotsix/robotsix-central-deploy.git
cd robotsix-central-deploy
uv sync                 # install all dependencies (runtime + dev)
pre-commit install      # install git pre-commit hooks
```

> **Note:** [uv](https://docs.astral.sh/uv/) is required — plain `pip install`
> is not supported because some dependencies are resolved from git sources
> pinned in `uv.lock`.

## Running tests

```bash
uv run pytest                    # full suite
uv run pytest tests/lifecycle/   # single module
uv run pytest -k "test_name"     # filter by name
```

Tests that require a real Docker daemon are marked with `@pytest.mark.docker`
and skipped by default in CI.  Run them locally with:

```bash
uv run pytest -m docker
```

## Linting & type checking

```bash
ruff check .                     # lint
ruff format . --check            # check formatting
ruff format .                    # auto-format
uv run mypy src/ --strict        # type check
deptry .                         # import / dependency hygiene
```

All of these run in CI (and as pre-commit hooks if you ran `pre-commit install`).
Pull requests that fail any check will be blocked.

## Pull request workflow

1. Create a feature branch from `main`.
2. Make your changes, including tests for new behaviour.
3. Run the full pre-commit suite: `pre-commit run --all-files`
4. Add a changelog fragment in `changelog.d/` (see the
   [towncrier](https://towncrier.readthedocs.io/) config in `pyproject.toml`).
5. Open a PR against `main`. The CI pipeline runs lint, mypy, deptry, and the
   full test suite — make sure everything is green.

## Coding standards

- Python ≥ 3.14 only (PEP 758, modern syntax).
- All public APIs must have type annotations (`mypy --strict` is the gate).
- Formatting is handled by `ruff format`; lint rules are in `[tool.ruff.lint]`
  in `pyproject.toml`.
- New modules must be registered in [`docs/modules.yaml`](docs/modules.yaml).
- Test files belong under `tests/<module>/`, never at the `tests/` root.
- Keep the [service state machine](AGENT.md#service-state-machine) contract in
  mind — invalid transitions must return **409 Conflict**.

## Architecture

For a detailed walkthrough of the codebase, see
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
