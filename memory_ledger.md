# Memory ledger — robotsix-central-deploy

## Repo layout (docs)
- Top-level docs: `README.md`, `ARCHITECTURE.md`, `CHANGELOG.md`, `CONTRIBUTING.md`, `SECURITY.md`.
- Per-module docs under `docs/<module>/`: e.g. `docs/lifecycle/api.md` (API reference with `::: robotsix_central_deploy.<module>` mkdocstrings directives), `docs/lifecycle/configuration.md`, plus `docs/ui/DEPLOY_CONTRACT.md`.
- `docs/modules.yaml` maps each module → source/tests/docs paths.
- Changelog: `CHANGELOG.md` built via towncrier; fragments live in `changelog.d/*.md`.
- Config sources (all in `config/`): `config.json` (live/default), `config.example.json` (example), `config.schema.json` (JSON schema).

## Conventions
- Adding a new public `.py` module requires a `::: robotsix_central_deploy.<path>` directive in `docs/api.md` (or the matching per-module api doc, e.g. `docs/lifecycle/api.md`).
- Doc-quality gates: markdownlint-cli2, codespell, TruffleHog.

## Component roster (virtual components)
- Live/default roster that backs `GET /chat/components`: `config/config.json` → `LifecycleConfig.virtual_components`. Each entry: `id`, `chat_base_url`, `chat_skill_endpoint`, `chat_skill` (empty = probe `/chat-skill` live), `auth_type` (`header` etc.), `auth_header_name`/`auth_token_env` for header auth, `auth_username_env`/`auth_password_env` for basic auth.
- `config/config.json` currently registers: langfuse, deploy, github, and cost-monitor (`http://cost-monitor:8200`, skill at `/chat-skill`).
- NOTE: `config/config.example.json` is stale — it lists only langfuse + deploy and is missing both `github` and `cost-monitor`. Not fixed by this ticket (pre-existing drift from the github addition). If a future ticket touches the roster, consider syncing the example file.
- The JSON schema (`config.schema.json`) does not hardcode component ids — id is free-form `^[a-z0-9][a-z0-9-]*$`, so adding a component needs no schema change.
- Chat agent self-restarts via `POST /chat/services/chat/restart` (per startup log reminder) to pick up roster changes.
