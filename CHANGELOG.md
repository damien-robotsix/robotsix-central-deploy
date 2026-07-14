# Changelog

All notable changes to robotsix-central-deploy.

<!-- towncrier release notes start -->

## 0.0.0 (unreleased)

- Extract remaining inline `style="display:none"` attributes from `dashboard.html` into CSS rules, and update JS `style.display = ''` calls to explicit display values so the CSS rules are properly overridden at runtime.
- Complete UI refactor: extract inline `<style>` and `<script>` blocks from `ui/login.html` into `ui/static/login.css` and `ui/static/login.js`; replace `get_deploy_contract()` inline HTML string concatenation with `ui/deploy-contract.html` template; replace ~110 inline `style="..."` attributes in `dashboard.html` with CSS classes defined in `dashboard.css`; replace all 31 inline event handlers (`onclick`, `onsubmit`, `onchange`) with `addEventListener` bindings in `dashboard.js` via `wireEventListeners()`.
- Extract shared `_call_github_endpoint` helper in `chat_github.py` to eliminate duplicated try/except/raise boilerplate across 9 GitHub endpoint handlers. Read handlers use the helper directly; write handlers additionally pass an `audit_entry` for audit logging on success.
- Extract shared `_read_and_parse_credentials` helper in `_auth_ops.py` to eliminate duplicated volume-exist → read → parse orchestration in `check_claude_auth` and `read_claude_credentials`.
- Added `GET /chat/github/repos/{owner}/{repo}/actions/permissions/workflow` and `PUT` endpoints to read and set default workflow permissions (including `can_approve_pull_request_reviews`).
- Extended `PATCH /chat/github/repos/{owner}/{repo}` to accept `allow_auto_merge` and `delete_branch_on_merge`, and reject unknown keys with 422.
- Rate-limit deploy-job and onboard-job poll intervals from 1.5 s to 5 s to reduce 404 noise when the server restarts and loses in-memory job state.
- Add `POST /chat/github/repos/{owner}/{repo}/pulls/{number}/merge` endpoint for merging (or merge-queuing) pull requests via the GitHub App installation token. Optional `merge_method` and `sha` guard are passed through to GitHub. When the repository requires a merge queue, the endpoint falls back to a raw API requester to enqueue the PR. Returns 404 for repos the credential doesn't cover, 405 if merge is not allowed, 409 on conflicts, 422 for GitHub-side rejections, and 503 when the App is not configured. The github component skill doc now includes the endpoint with an explicit 🛑 confirmation-gate safety rule.
- Extract duplicated volume-write boilerplate from ``write_config_to_volume`` and ``write_llmio_tier_config_to_volume`` into private ``_write_json_to_volume`` helper
- Replace unmaintained `starlette-csrf` with actively maintained `asgi-csrf` (v0.11) for CSRF protection. The `GatewayAwareCSRFMiddleware` pattern (skipping CSRF for gateway-proxied subdomain requests) is preserved.
- Extract ``_read_volume_credentials`` helper in ``_auth_ops.py``, deduplicating the busybox container-run boilerplate shared between ``check_claude_auth`` and ``read_claude_credentials``. (mill: robotsix-central-deploy: Complete UI refactoring — remaining inline code after initial extraction (20260714T052616Z-robotsix-central-deploy-complete-ui-refa-ac7e) [WIP])
- Guard `starlette-csrf` and `itsdangerous` imports so the lifecycle server
  remains importable (and the CSRF feature degrades gracefully) when those
  optional packages are not installed in the environment.
- Add CSRF protection: ``starlette-csrf`` middleware with cookie-based token
  validation for browser-facing routes, manual token injection/validation on
  the login form, and hidden CSRF fields in the dashboard settings form.
  API routes authenticated via ``X-API-Key`` header are exempt from CSRF
  checks since bearer-style auth is not vulnerable to CSRF.
- Add SecureASGIMiddleware with the BALANCED preset for security headers (CSP, HSTS, X-Frame-Options, etc.)
- Eliminate `SiblingDerivedSpec` field duplication by making it a type alias for `ServiceConfig` from `registry.models`
- Extract shared sibling fan-out plumbing in ``services_deploy.py``: the common sibling-iteration preamble from ``_fanout_deploy_siblings`` and ``_fanout_rollback_siblings`` is now a single ``_fanout_sibling_action`` helper that accepts a per-sibling action callback, eliminating ~40 lines of duplicated boilerplate.
- Consolidate `OnboardJob` and `DeployJob` into a single `Job` base class with `ClassVar[type[Enum]]` for the phase type, and fold the five `_deploy`-prefixed `JobRegistry` methods into the base method names by reading the phase type from the job instance.
- Remove orphaned `_gen_openapi_tmp.py` build artifact (committed by PR #425) and add to `.gitignore`
- Added `PUT /chat/github/repos/{owner}/{repo}/security-features` endpoint so the chat agent can toggle Dependency Graph, Dependabot alerts, and Dependabot security updates in one call instead of asking the operator to manually toggle them in GitHub's web UI. Uses App installation token with PAT fallback; returns the resulting `security_and_analysis` state.
- Add server-side auth-injecting Langfuse proxy (`/chat/langfuse/api/public/...`) so the chat container no longer needs `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY` in its own environment. The deploy server injects HTTP Basic Auth from `langfuse_chat_public_key`/`langfuse_chat_secret_key` (or cognee equivalents via `?project=cognee`) when proxying read-only Langfuse public-API requests, mirroring the auth-injection pattern used by the `github` virtual component.
- Replace inline fetch-repo-files preamble in `refresh_contract` with a call to the shared `_fetch_component_repo_files` helper, removing duplicated code.
- Deduplicate `_sanitize_log` helper: routers `services_deploy` and `services_config` now import it from `_config_utils` instead of redefining it locally; `_sibling_utils` also uses the shared function.
- Extract `_lifecycle_action` shared helper in `services.py`, consolidating ~225 lines of boilerplate from `start_service`, `stop_service`, and `restart_service` into a single parameterised implementation.
- Added `/chat/preview/deploy` and `/chat/preview/teardown` endpoints for deploying an arbitrary repo+branch into a single reusable preview slot, served at `preview.<gateway_base_domain>`.)
- Added dedicated unit tests for `AuthOps` covering `check_claude_auth`, `write_claude_credentials`, and `read_claude_credentials` with mocked Docker SDK.
- Add `robotsix-modules` taxonomy validation to CI and pre-commit hooks to prevent `docs/modules.yaml` drift
- Propagate `mem_limit` from docker-compose.yml through the full lifecycle pipeline: add `mem_limit` to `DerivedSpec`, wire it through `_build_component_config_from_spec`, and include it in `_CONTRACT_FIELDS` so contract-refresh detects changes. Previously the primary service's `mem_limit` was silently discarded, always defaulting to `"2g"`.
- Extract duplicated sibling fan-out boilerplate from ``services.py`` and ``chat.py`` into a shared ``_fanout_siblings_best_effort`` helper in ``lifecycle/routers/_sibling_utils.py``. Also wrap each sibling deploy in ``onboard.py``'s ``_deploy_onboard_siblings`` in per-sibling try/except for best-effort resilience.
- Document the rule for keeping `docs/api.md` up to date: when adding a new public `.py` module, add a corresponding `::: robotsix_central_deploy.<module_path>` mkdocstrings directive under the matching section.
- Extract volume ops (`_volume_ops.py`) and Claude-auth ops (`_auth_ops.py`) from
  `lifecycle/backends/docker_sdk.py`, reducing the file from 1,416 to ~945 lines.
  The new `VolumeOps` and `AuthOps` helper classes share the Docker client with
  `DockerSdkBackend` via constructor injection.
- Add missing mkdocstrings `:::` directives to `docs/api.md` for ~35 source modules (lifecycle backends, deps, routers, internals; caretaker; deploy lock; onboard port_utils; registry audit/deploy history stores), completing the auto-generated API reference coverage.
- Extract shared `_build_component_config_from_spec` factory in `lifecycle/deps/seed.py`, deduplicating the `ComponentConfig` construction from `DerivedSpec` that was copy-pasted between onboard-confirm and contract-refresh handlers.
- Extract duplicated repo-fetch preamble from `services_config` and `services_env` routers into a shared `_fetch_component_repo_files` helper in `lifecycle/deps/seed.py`.
- Split `lifecycle/deps.py` (1,379 lines) into a `lifecycle/deps/` sub-package with focused modules: `background.py` (Claude auth + registry check loops), `jobs.py` (OnboardJob, DeployJob, JobRegistry), `lifespan.py` (init/teardown), `dependencies.py` (FastAPI `_get_*` providers), `seed.py` (onboard-seed helpers), and `volume.py` (volume utilities). All existing imports from `lifecycle.deps` continue working unchanged.
- Split monolithic `dashboard.html` into separate CSS (`ui/static/dashboard.css`), JS (`ui/static/dashboard.js`), and a thin HTML shell that loads both via `<link>` and `<script>` tags. Added `/ui/static/{filename}` route to serve the extracted static assets.
- Wire `mem_limit` through the docker-compose parser so sibling services respect the compose file's `mem_limit` field instead of always defaulting to `"2g"`.
- Add `docs/registry/overview.md` documenting the component registry module (models, stores, secrets, and architecture).
- Add `docs/gateway/overview.md` documenting the reverse-proxy gateway (subdomain routing, WebSocket relay, legacy path redirection, reserved names, and configuration).
- Add `docs/caretaker/overview.md` documenting the caretaker background maintenance agent (architecture, phases, finding model, configuration, API, and reporting).
- Add `docs/onboard/overview.md` documenting the onboard-from-git two-phase workflow, architecture, deploy contract, and API endpoints.
- Align `SiblingDerivedSpec` field names with `ServiceConfig`: rename `volume_mounts` → `mounts` and add `mem_limit: str = "2g"`
- Added CONTRIBUTING.md, SECURITY.md, and .gitattributes as standard repository files (per robotsix-standards convention).
- Refactor `DockerSdkBackend.deploy()`: extract `_remove_old_container`, `_prepare_volumes`, and `_try_restore` helpers to reduce nesting and improve readability.
- Extract duplicated `ComponentConfig` construction in sibling fanout helpers into `_build_sibling_config`
- Add ``pytest.importorskip("github")`` guards to ``tests/lifecycle/routers/test_chat_github.py`` and ``tests/lifecycle/test_github_app.py`` so the test suite skips gracefully when PyGithub is unavailable in the test environment.
- Fixed ``AttributeError`` in ``PUT /settings`` when ``llmio_tier_config`` changes: corrected ``app.state.execution_backend`` to ``app.state.backend`` in the tier-config propagation block of ``settings_router.py``.
- Register `tests/caretaker/` test files under the caretaker module in `docs/modules.yaml`.
- Classify `tests/ui/` test files under the `ui` module in `docs/modules.yaml` with `tests/ui/**/*` path glob.
- Add `tests/lifecycle/**/*` glob to lifecycle module paths in `docs/modules.yaml` to claim all lifecycle test files
- Classify `tests/registry/` test files under the `registry` module in `docs/modules.yaml`.
- Add `.badge-unknown` CSS class to dashboard for consistent grey styling on unknown-state badges
- Remove dead code: ``_fetch_fresh_config_assist`` (zero call sites, no tests).
- Standardize pseudo-enums as ``StrEnum`` classes in ``lifecycle/models.py``: ``ActionType``, ``DeploySource``, ``OnboardJobPhase``, ``DeployJobPhase``. Replace raw string literals across services, chat, onboard, deploy, deps, and caretaker modules with enum member references. Fix dashboard deploy-history source badge to match ``"manual"`` instead of ``"deploy"``.
- Deactivate all periodic mill workflows: removed every `.yaml` file under `.robotsix-mill/periodic/` to pause auto-generated tickets (audit, survey, completeness_check, test_gap, security_posture, etc.) until the board backlog is under control.
- Extract duplicated llmio tier config write pattern into ``_write_llmio_tier_config`` helper in ``lifecycle/_config_utils.py``, replacing the two inlined copies in ``put_service_config`` and ``_run_deploy_job``.
- Refactor deeply-nested functions in `lifecycle/deps.py`: extract `_check_and_update_record`, `_refresh_claude_credentials`, and `_seed_list_item` helpers to reduce nesting depth in `_registry_check_loop`, `_claude_auth_refresh_loop`, and `_seed_for_detect`.
- Expose volume-audit tuning knobs (`volume_audit_enabled`, `volume_audit_interval_seconds`, `volume_audit_growth_threshold_pct`, `volume_audit_min_delta_bytes`) in ``SystemSettings`` and the ``GET/PUT /settings`` endpoints so operators can configure them without restarting the server.
- Mirror lifecycle source-tree structure in tests: move router tests to `tests/lifecycle/routers/` and backend tests to `tests/lifecycle/backends/`
- Moved `docs/architecture/registry_check.md` to `docs/registry_check/overview.md` to align registry_check with the per-module docs pattern.
- Classify `tests/__init__.py` and `tests/deploy_lock/__init__.py` under the lifecycle module in `docs/modules.yaml`
- Register top-level `docs/` files (`ARCHITECTURE.md`, `api.md`, `changelog.md`, `configuration.md`, `deployment.md`, `index.md`, `nginx-deploy.conf`, `openapi.json`) under the `lifecycle` module in `docs/modules.yaml`.
- Dashboard UX: rename "Deploy" button to "Env &amp; Secrets" to reflect that it edits env/secrets, not deploy config. Add raw-JSON toggle in the Configure modal so users can paste/edit the full config document as JSON alongside the generated form.
- Fix: the `robotsix_central_deploy` logger no longer hard-codes `INFO` level, so `PUT /settings` with a new `log_level` now correctly hot-applies to application log messages (logger level set to `NOTSET` to inherit from root).
- Add structured JSON logging via structlog: uvicorn access logs and application logs now emit JSON to stdout, while the uvicorn startup banner remains human-readable. Configured through a shared `LOGGING_CONFIG` dict in `lifecycle/_logging.py`.
- Add unit tests for onboard `port_utils` module (host-port collision helpers).
- PUT /settings now supports partial updates: only fields explicitly present in the request body are changed; unmentioned fields keep their current stored values. This prevents a partial payload from silently resetting fields like ``gateway_base_domain`` or ``caretaker_enabled`` to their class defaults.
- Document `caretaker/` subpackage in ARCHITECTURE.md (FindingKind enum, CaretakerFinding + CaretakerReport models, phase functions, scheduler).
- Extract duplicated Docker-status-to-ServiceState mapping into shared `docker_status_to_service_state` helper in `lifecycle/backends/_util.py`.
- Refactor `ChatAgentAuditStore` to inherit from `JsonFileStore`, replacing hand-rolled `_load`/`_save`/lock with the inherited `_update()` pattern used by other JSON-file-backed stores (`ConfigYamlStore`, `EnvStore`, `DeployHistoryStore`).
- Added `docs/volume_audit/overview.md` documenting the volume audit subsystem: architecture (scheduler → growth → reporter pipeline), threshold model, configuration env vars, API endpoint, and reporting behaviour.
- Remove dead ``DeployResponse`` model from ``lifecycle/models.py`` (gap-003). The deploy endpoint was converted from synchronous to asynchronous (202 + polling), making this legacy response model unused by any router, test, or import.
- Rate limiter: raise the duplicated `SystemSettings.rate_limit_api_per_hour`
  default (registry/settings_store.py) 1000 -> 20000. The settings overlay
  stamps this value over `app.state.config` at startup, so it silently
  overrode the raised LifecycleConfig default from #331 and kept 429-locking
  the dashboard.
- Add virtual (non-Docker) component support to the chat-agent component roster: `chat_base_url`, `chat_skill_endpoint`, `chat_skill`, and auth-metadata fields (`auth_type`, `auth_header_name`, `auth_username_env`, `auth_password_env`, `auth_token_env`) on `ComponentConfig` and `VirtualComponentEntry`. The deploy server exposes `GET /chat-skill`, seeds `langfuse` (basic-auth via `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`) and `deploy` (X-API-Key header via `DEPLOY_API_KEY`) as virtual components from `config.json`, and includes auth env-var references in the roster response so the chat agent can authenticate without plaintext credentials. A startup log message reminds operators to restart `robotsix-chat` after roster changes.
- Rate limiter (#318 follow-up): gateway-proxied component traffic
  (`<name>.<gateway_base_domain>` hosts) now bypasses the login/API rate
  limits — a component's own `/chat` or `/login` paths were being counted
  against and blocked by central-deploy's per-IP budget (2026-07-05: 429s
  on the dashboard AND on chat for the same client). Raised the default
  `rate_limit_api_per_hour` 1000 → 20000: the dashboard UI alone polls
  ~5000 requests/hour per open tab, so the old default locked out an
  operator within minutes.
- Update `docs/ARCHITECTURE.md` to reflect the `claude-auth` named volume (managed by central-deploy) instead of the old `~/.claude` host bind-mount.
- docs: update DEPLOY_CONTRACT.md §5, §9, and §10 to reflect managed `claude-auth` named volume instead of host bind-mount; fix dashboard.html claude-mount label to show correct volume name and container path
- Register the previously uncovered `src/robotsix_central_deploy/__init__.py` under the lifecycle module in `docs/modules.yaml`.
- Move `volume_audit` module from `src/robotsix_central_deploy/lifecycle/volume_audit/` to `src/robotsix_central_deploy/volume_audit/` as a top-level peer package, and tests from `tests/lifecycle/volume_audit/` to `tests/volume_audit/`.
- Extend `.pre-commit-config.yaml` with seven additional hooks from the standard set: `check-yaml`, `check-toml`, `check-json`, `detect-private-key`, `detect-secrets`, `actionlint`, `hadolint`, and `vulture`. Generate initial `.secrets.baseline`.
- Add `lint-actions` CI job with actionlint (structural workflow validation) and zizmor (supply-chain vulnerability detection) for all GitHub Actions workflow files
- Extract shared test fixtures (``app``, ``_reset_globals``, ``client``) into a root ``tests/conftest.py``, deduplicating ~155 lines of boilerplate across five test locations.
- Move `DEPLOY_CONTRACT.md` from `src/robotsix_central_deploy/ui/` and `docs/` to `docs/ui/DEPLOY_CONTRACT.md`, aligning the ui module docs with the per-module layout convention (`docs/<module>/`)
- Skip-Changelog: boilerplate triage template, no code change
- Dashboard: fallback state after fetch failure now renders `unknown` (valid `ServiceState`) instead of `error` (non-canonical), with `badge-unknown` CSS class instead of removed `badge-error`.
- Split `lifecycle/routers/services.py` (~2450 lines) into focused router modules under `routers/`: `services_deploy.py` (deploy, rollback, history), `services_config.py` (config CRUD, assist, schema refresh), and `services_env.py` (env/secrets CRUD). The core `services.py` retains listing, status, health, logs, start/stop/restart, refresh-contract, and delete.
- Extract ``JsonFileStore`` base class into ``registry._store_utils``, consolidating the duplicated ``asyncio.Lock`` + ``_load``/``_save`` boilerplate from ``ConfigYamlStore``, ``EnvStore``, and ``DeployHistoryStore``.  Adds an ``_update(mutator)`` convenience helper for the common read-modify-write pattern.
- Add per-IP rate limiting middleware protecting the login endpoint (10 req/min) and authenticated API paths (1000 req/hour). Configurable login lockout after repeated failures. All limits adjustable via LifecycleConfig and the /settings API.
- Fixed chat agent config rollback: first update now snapshots template defaults instead of the raw JSON Schema, and `release_deploy_lock` is no longer incorrectly awaited. Added `ComponentConfigStore.register()` synchronous helper for test fixtures.
- Chat agent scoped write-surface: new endpoints for allowlisted service
  mutation — update non-secret config keys, rollback config, restart
  services, and pull+recreate (deploy). Every mutation is audit-logged
  with who/what/when/old→new value. Includes server-side allowlist
  enforcement (403 on disallowed services/keys) and per-service
  per-action rate limiting.
- When central-deploy creates a named volume for the first time, chown its root to the container's uid:gid (mode 0755, or 0700 for claude-auth). This fixes PermissionError on data persistence for components running as a non-root user. The claude-auth special-case volume helper is retired in favour of the general mechanism.
- Lazily initialise Docker client in `DockerSdkBackend` to prevent import/construction failures when the Docker daemon is unreachable (defer `DockerClient(...)` to first use via a `_docker_client` cached property)
- Re-export ``DockerSdkBackend``, ``NoopBackend``, and ``collect_protected_image_refs`` from ``lifecycle/__init__.py`` so they are importable from the root package path.
- Add public API facade to `volume_audit` package with `__all__` re-exports matching sibling subpackage conventions.
- Add docstrings to caretaker models: ``FindingKind``, ``CaretakerFinding``, and ``CaretakerReport``.
- Register `docs/architecture/registry_check.md` under the `registry_check` module in `docs/modules.yaml`.
- Removed unused `spec_config_schema` parameter from `_run_onboard_deploy_job` in `lifecycle/routers/onboard.py` — it was never referenced in the function body.
- Add missing docstrings to 8 public methods of ``DockerSdkBackend`` (``status``, ``start``, ``stop``, ``remove_container``, ``restart``, ``measure_volume_bytes``, ``stream_logs``, ``disk_df``).
- Made dashboard deploys asynchronous: ``POST /services/{name}/deploy`` now returns ``202 Accepted`` with a ``job_id`` immediately, while the deploy runs as a background job. Added ``GET /services/deploy-jobs/{job_id}`` for polling job progress. The dashboard UI now polls and renders deploy-phase labels (deploying / waiting-health / deploying-siblings) instead of blocking on a frozen button. When an API-initiated deploy job is already active for a component, a second request returns the existing ``job_id`` instead of ``409 Conflict``.
- Serialise concurrent deploys per component: when a deploy is already in
  progress for a component (e.g. caretaker auto-update), operator-initiated
  deploys now receive 409 "Deploy already in progress" instead of racing
  into Docker. The caretaker skips auto-deploy when an operator deploy holds
  the lock, retrying on the next cycle.
- Add ``.github/workflows/docs.yml`` to build and deploy docs to GitHub Pages on every push to main, using the shared ``python-docs.yml`` reusable workflow.
- Extract config-merge helpers from ``lifecycle/deps.py`` into new ``lifecycle/_config_utils.py`` module, and move ``_deep_merge`` from ``services.py`` into the same module. This makes the merge logic independently testable and reduces ``deps.py`` by ~300 lines.
- Eliminate duplicated `OnboardJobPhase` literal type alias; `deps.py` now imports it from `schemas.py`
- Extract shared ``async_read_json`` / ``async_write_json`` helpers into ``registry._store_utils``, deduplicating the tmp-file-rename persistence pattern across ``ConfigYamlStore``, ``EnvStore``, and ``DeployHistoryStore``.
- Add missing `deploy_history_store_path` and `llmio_tier_config` fields to `config/config.example.json` (skip `claude_auth_helper_image` — already removed).
- Remove dead constant `RESTING_STATES` from `lifecycle/models.py` — never imported or referenced anywhere.
- Background credential refresh for the claude-auth volume: periodically checks `.credentials.json` and POSTs a refresh_token grant to the Anthropic OAuth token endpoint when the access token is within 1 hour of expiry. Uses a CLI-like User-Agent to avoid Cloudflare 403, persists the rotated refresh token atomically (0600, 1000:1000), and surfaces refresh success/failure in the Claude Auth dashboard panel. Configurable via `claude_auth_refresh_interval` (default 1800 s; 0 disables).
- Remove fabricated llmio_tier_config defaults (openai/gpt-4o-mini, etc.) — default to empty per-level entries that inherit robotsix-llmio's baked defaults. The settings UI now shows the baked defaults (openrouter-deepseek/deepseek-v4-flash, …/deepseek-v4-pro, claudeSDK/opus, claudeSDK/claude-fable-5) as grey placeholder text. Also remove the stale `claude_auth_helper_image` setting (obsoleted by the PKCE login rewrite).
- Show both **Configure** (schema-driven) and **Deploy** (env/secrets/mem_limit/chat_access) buttons for every component, so schema components no longer lose access to deploy-specific settings.
- Add **Claude Mount** toggle to the Deploy settings modal (takes effect on next deploy).
- Add Hypothesis property-based tests for lifecycle state machine, input validation fuzzing, and enum roundtrip serialisation
  Add `hypothesis>=6.0` to dev dependencies and create `tests/lifecycle/test_hypothesis.py` with three test groups:
  `RuleBasedStateMachine` for state-transition completeness, `@given`-based fuzzing of log endpoint query parameters (`tail`, `since`, `follow`),
  and parametrised roundtrip + rejection tests for all six `str, Enum` models.
- Honor docker-compose ``user:`` key: when a service declares a user override (e.g. ``user: root``), the Docker SDK backend uses it instead of the forced host UID:GID. Fixes third-party sibling containers (tecnativa/docker-socket-proxy) that require their image-default user.
- Add tmpfs support to compose parser, ComponentConfig model, and container creation backend. tmpfs mounts declared in deploy/docker-compose.yml now flow through to Docker HostConfig.Tmpfs, fixing the mill-socket-proxy crash-loop after contract refresh. Also add regression tests for multi-line list-form command preservation using the mill-socket-proxy contract as a fixture.
- Added ``POST /services/{name}/refresh-contract`` endpoint that re-fetches
  ``deploy/docker-compose.yml`` from the component's git repo and updates the
  stored ``ComponentConfig`` with fresh contract-derived settings (image, ports,
  mounts, command, entrypoint, health check, siblings, labels, etc.).  Operator-set
  fields (repo_id, caretaker_auto_update, mem_limit) are left untouched.  Returns
  which fields changed so the operator can decide whether a redeploy is needed.
- Enable triage-boilerplate periodic workflow via `.robotsix-mill/periodic/triage_boilerplate.yaml` presence file.
- Config UI: suggest peer-component URLs for `*_url` / `*_base_url` fields. A 🔍 button next to matching config fields opens a dropdown of registered components; picking one fills `http://<container_name>:<first container port>`. The field-name prefix is used as a hint to preselect the matching component (e.g. `mill_url` → component `mill`).
- Caretaker: distinguish mill reachability failure modes (not registered / no ports / health probe failed) and use a lightweight GET /health probe instead of inferring reachability from ingest success.
- DELETE /services/{name} is now idempotent and atomic: it purges ServiceRecord(s), env, config YAML, and component config even when the component config store entry is already absent. Sibling/helper services (e.g. ``<name>-socket-proxy``) discovered by prefix scan are also torn down.
- Per-component chat access control: `ComponentConfig.allow_chat_access` flag (defaulted from compose label `robotsix.deploy.chat-access`) with onboarding UI checkbox and post-onboard toggle. New `GET /chat/components` roster endpoint for the chat agent, returning `{id, base_url, skill}` with a 60s skill-body cache.
- Remove dead `__getattr__` and `__all__` from `caretaker/__init__.py` — all callers already import `CaretakerScheduler` directly from `caretaker.scheduler`.
- Fleet-global LLMIO tier configuration: add ``llmio_tier_config`` to system settings (dashboard-editable mapping from capability level1-4 to provider+model), ``llmio_tier_level`` to ``ComponentConfig`` (declared via ``robotsix.deploy.llmio-tier-level`` docker-compose label). Deploy, config-update, and onboard flows now write the full tier mapping into each LLM component's config volume as ``llmio_tier_config.json``, and ``PUT /settings`` propagates tier config changes to all LLM components immediately. Changing the global mapping affects every component on its next deploy with zero per-repo changes.
- Remove deprecated `lifecycle/backend.py` shim module; all callers (caretaker, tests) now import directly from `lifecycle.backends` or `lifecycle.models`
- Onboard: seed the component config from `config/config.example.json`'s values (precedence: user input > example > schema default), so deploy-appropriate example values (e.g. `api_host: 0.0.0.0`) are honored instead of the code's schema default. Secret leaves from the example are stripped.

- Migrate onboarding config handling from YAML naming to JSON: rename ``config_yaml`` / ``config_yaml_template`` fields to ``config_json`` / ``config_json_template`` in ``RepoFiles``, rename ``parse_config_yaml`` to ``parse_config_json``, and update all docstrings/comments referencing ``config.yaml`` to ``config.json``.
- Migrate onboarding config handling from YAML naming to JSON: rename ``config_yaml`` / ``config_yaml_template`` fields to ``config_json`` / ``config_json_template`` in ``RepoFiles``, rename ``parse_config_yaml`` to ``parse_config_json``, and update all docstrings/comments referencing ``config.yaml`` to ``config.json``.
- Docker build: accept an optional `github_token` build secret to authenticate git
  operations when resolving private/rate-limited GitHub dependencies during
  `uv export --frozen`. The CI trivy job now passes `GITHUB_TOKEN` as a build
  secret so the image can be built for vulnerability scanning.
- Apply a default memory limit (2g) to every managed container at create time, overridable per component via the dashboard Config modal. The limit is applied on the next deploy/rollback.
- Add `llmio_tier_config` to system settings — a fleet-global mapping from capability level1-4 to provider+model, editable in the dashboard. Components declare their tier via `robotsix.deploy.llmio-tier-level` label (exposed as `ComponentConfig.llmio_tier_level`), and central-deploy writes the resolved `llmio_tier_config.json` into the config volume at deploy/config-update time. Changing the global mapping propagates on next deploy with zero per-component repo changes.
- Fleet-global LLMIO tier configuration: add `llmio_tier_config` to system settings (dashboard-editable mapping from capability level1-4 to provider+model), `llmio_tier_level` to `ComponentConfig` (declared via `robotsix.deploy.llmio-tier-level` docker-compose label), and write the resolved tier config as `llmio_tier_config.json` into each LLM component's config volume at deploy time. Changing the global mapping affects every component on its next deploy with zero per-repo changes.
- Classify `_yaml_utils.py` under the lifecycle module in `docs/modules.yaml`; restructure manifest from dict to list format with `id` + `paths` entries so the `robotsix-modules check-registration` tool can parse it correctly.
- Move `tests/lifecycle/test_volume_audit_endpoint.py` into the `tests/lifecycle/volume_audit/` directory as `test_endpoint.py`, consolidating all volume_audit tests under one module-aligned test directory.
- Move `ContainerHealthSummary` import under `TYPE_CHECKING` in `lifecycle/deps.py` to avoid unnecessary runtime import
- Complete §8 transition: config is now JSON-Schema-driven (``config/config.schema.json`` + ``config/config.json``), secret detection via ``format: password`` + ``writeOnly: true``. Removed legacy YAML empty-leaf secret heuristic and ``_CONFIG_SECRET_SENTINEL`` support.
- Refactor 180-line lifespan function in ``lifecycle/deps.py`` into five private async helpers (``_init_config``, ``_init_settings``, ``_init_background_tasks``, ``_init_component_registry``, ``_teardown``) with individual docstrings, reducing the lifespan body to ~15 lines of orchestration.
- Added 'Claude auth' panel to the dashboard UI with interactive OAuth login, fallback credentials paste, and live credential-status reporting for the ``claude-auth`` volume.
- Add `claude-auth` named volume seed migration tests covering idempotent re-run, absent host source, and rmtree failure; clean `claude_host_mount_path` from OpenAPI spec and configuration docs.
- Replace host bind mount for `robotsix.deploy.claude-mount` with a central-deploy-managed named volume `claude-auth`. The `claude_host_mount_path` setting is removed; claude credentials now live in a Docker named volume mounted at `/home/app/.claude`. A one-time migration seeds the volume from `/home/debian/.claude` if present. Deploying a claude-mount component without valid credentials surfaces a dashboard warning.
- Remove the `robotsix.deploy.stateful` volume label and its associated onboarding gate. Named volumes start empty on first deploy; backups are the operator's responsibility. The label is now silently ignored (existing composes keep parsing).
- Fix startup ordering so ``system_settings.json`` overlay is applied *before* the backend is constructed.  Previously the backend was built from raw config, so restart-gated settings (``docker_socket_url``, ``docker_sdk_timeout``) never reflected the persisted overlay even after a full service restart.  The PUT ``/settings`` docstring now accurately reflects the corrected behavior.
- Host port auto-assignment at onboarding: when a new component's default host port collides with an existing component or central-deploy's own port, `onboard_preflight` auto-assigns a free port (10000-20000 range) and returns the shifts in `port_shifts`. Port collision tickets are filed on affected components' mill boards when reachable; unreachable-mill warnings are surfaced in the job status response and dashboard.
- Eliminate triple field duplication by deriving `SystemSettingsResponse` and `SystemSettingsUpdate` from `SystemSettings` instead of `BaseModel`.
- Migrate component-config onboarding to the JSON config-standard: fetch `config/config.json` + `config/config.example.json`, parse/write JSON (`config.json`) instead of YAML. Compose parsing stays YAML. (Pairs with each component's config-standard migration.)

- Dashboard: add deploy history modal with per-entry rollback controls and full running digest tooltips
- Refactor `lifecycle/backend.py` (1541 lines) into a `lifecycle/backends/` package
  with one file per implementation: `base.py`, `noop.py`, `docker_cli.py`,
  `docker_sdk.py`, and `_util.py`. The original `backend.py` is kept as a
  backward-compatible re-export shim.
- Fix caretaker auto-update pulling a bare digest: `phase_update` passed `latest_registry_digest` (`sha256:…`) as the image reference, which docker resolves as repository "sha256" and 404s — every auto-update failed on the live server. It now deploys `repo@sha256:…` (tag fallback when no digest), and `deploy()` digest derivation handles pinned refs so `update_available` clears correctly.
- Remove dead `OnboardConfirmResponse` schema (superseded by `OnboardConfirmAcceptedResponse`)
  Regenerated `docs/openapi.json` to reflect the active schema.
- Adopt towncrier newsfragments (`changelog.d/`) as the changelog mechanism; CHANGELOG.md becomes release-workflow-written only. Added `towncrier>=24.0` dev dependency and `[tool.towncrier]` config.
- Per-component deploy history: every successful deploy (manual, caretaker, or rollback) now records a timestamped entry with the resolved digest, image ref, source, and previous digest. The history is capped at 20 entries per component, exposed via `GET /services/{name}/history`, and rollback can target any recorded digest via `POST /services/{name}/rollback {"digest": "<sha256:...>"}`.
- Env/secrets modal: the key set is the repo's compose contract, not operator-editable — removed "+ Add variable"/"+ Add secret" and per-row delete. New "↻ Sync keys from repo" button (POST /services/{name}/env/sync-keys) seeds keys the contract added since onboarding (values never modified; undeclared stored keys reported, not deleted).
- Fix contradictory copy on unset secret fields in config forms: rows showed both "(required — set before deploy)" and "optional at onboard" at once. Unset secrets now read "(not set — can be saved later, needed to run)".
- **Breaking (claude-mount):** the `robotsix.deploy.claude-mount` bind target moved from `/root/.claude` to `/home/app/.claude`, matching the robotsix-standards standardized container layout (non-root user `app`, uid 1001, home `/home/app`). Images still running as `root` (e.g. robotsix-mill) must set `CLAUDE_CONFIG_DIR=/home/app/.claude` until they migrate to the standard layout.
- Config help bubbles now show the schema's field `description` (falling back to the dotted key path), and section headers render their description. Added descriptions to every `LifecycleConfig` field (regenerated `config/config.schema.json`). New `POST /services/{name}/config/refresh-schema` endpoint + "↻ Refresh schema" button in the Configure modal: refetches the repo's committed `config/config.schema.json` and replaces the stored template (values untouched), so components onboarded pre-schema pick up typed fields and descriptions without re-onboarding.
- Fix caretaker mill discovery URL: derive `http://{container_name}:{container_port}` instead of `http://localhost:{host_port}` — managed components publish no host ports, so the caretaker must reach the mill over the shared proxy network like the gateway does.
- Fix empty Configure modal for components onboarded before the schema-driven config UI (e.g. mill, mail): the typed renderer only understood JSON Schema and silently rendered nothing for stored legacy YAML templates. Legacy templates are now converted to a pseudo-schema client-side (SECRET sentinel → masked input, typed number/bool inputs), arrays render as editable JSON instead of `[object Object]`, and the onboard config step recognises legacy-template repos again.
- Fix deployment breakage from the robotsix_config migration: `docker-compose.yml` still set the now-ignored `ROBOTSIX_LIFECYCLE_*` env vars, so a pulled post-migration image started with baked-in defaults (unix docker socket, memory store) and crash-looped. Compose now sets `ROBOTSIX_CONFIG_FILE=/data/config.json`; docs/deployment.md documents seeding the file.
- Add Dependabot auto-merge caller workflow (`.github/workflows/dependabot-auto-merge.yml`)
- Regenerate `config/config.json` and `config/config.example.json` to include `mill_component_id` and `image_auto_prune` defaults.
- **Breaking:** `LifecycleConfig` migrated from `pydantic-settings` env-var loading to `robotsix_config.load_config` (JSON file). Operators must replace `config/config.json` with a deployment-specific file containing real secrets — the committed version carries safe empty-string defaults. Added `config/config.json`, `config/config.example.json`, and `config/config.schema.json` with a CI drift check.
- Migrate from `robotsix-yaml-config` to `robotsix-config` dependency; YAML primitives (`read_yaml_file`, `deep_merge`, and the exception types) replaced with a local `_yaml_utils` module backed by `pyyaml`.
- Render typed, validated inputs in the config form driven by JSON Schema (`config.schema.json`): number inputs for `integer`/`number`, checkboxes for `boolean`, dropdowns for `enum`, password inputs for `format:password` + `writeOnly:true`, text inputs for plain strings, and section groups for nested `object` types. Required fields are marked with `*` and defaults from `propSchema.default` are prefilled. Secret detection now uses schema metadata (`format` + `writeOnly`) instead of the old `"SECRET"` sentinel value.
- Migrate config handling from YAML-sentinel templates to JSON Schema: onboard
  preflight now fetches `config/config.schema.json`, secrets are detected via
  `format: password` + `writeOnly: true`, and submitted config is validated
  against the schema before writing to the container volume. The old
  `_CONFIG_SECRET_SENTINEL` / `_annotate_secret_sentinels` path is removed.
- Fix `inspect_self` losing track of the server's own container after a watchtower self-update: the recreated container keeps the *previous* container's hostname (watchtower copies the config verbatim), so the container-id hostname lookup missed and `GET /system/update` reported `supported: false` until the next compose recreate. A fallback now scans running containers for a matching `Config.Hostname`.
- **Fix self-update failing on first real use.** The one-shot watchtower launched by `POST /system/update` crashed twice: (1) watchtower 1.7.1's Docker client defaults to API 1.25, below modern daemons' minimum (1.44), panicking on the first API call — the updater now receives `DOCKER_API_VERSION` (`ROBOTSIX_LIFECYCLE_SELF_UPDATE_DOCKER_API_VERSION`, default `1.44`); (2) recreating the central-deploy container 403'd because watchtower re-attaches networks via `POST /networks/{id}/connect`, which the socket proxy blocked — the compose socket-proxy scope now sets `NETWORKS: "1"`. Found live on server.robotsix.net; the failed run left the old container stopped, so deployments should update the socket-proxy (`docker compose up -d`) when picking this up.
- Dashboard: add caretaker settings (enable/disable, interval) to System Settings panel, mill tracking opt-in to onboard flow, untracked badge for components without repo_id, and degraded-reporting banner when caretaker is enabled but mill is unreachable.
- Enable `changelog_autofill` periodic workflow to automate changelog entry insertion for PRs missing the `changelog` check.
- **Dashboard self-update button.** New `GET/POST /system/update` endpoints: GET compares the running server's image digest (resolved from its own container via the container-id hostname) against the registry; POST launches a one-shot watchtower container (`ROBOTSIX_LIFECYCLE_SELF_UPDATE_WATCHTOWER_IMAGE`, default `containrrr/watchtower:1.7.1`) that pulls the new image and recreates the central-deploy container from outside the process. The watchtower container joins the server's own networks (the socket proxy blocks `/networks/*/connect`, so all endpoints are attached in the create payload) and auto-removes when done. The dashboard header shows an "⬆ Update server" button when an update is available; after confirming, it polls until the recreated server answers with a new digest, then reloads. `system` is now a reserved component name.
- Expose `caretaker_enabled` and `caretaker_interval_hours` as `ROBOTSIX_LIFECYCLE_CARETAKER_ENABLED` / `ROBOTSIX_LIFECYCLE_CARETAKER_INTERVAL_HOURS` env vars, seed them on first boot, and round-trip them through `GET/PUT /settings`.
- **Publish the image to GHCR and harden the Dockerfile per the docker standard.** A new `release.yml` calls the shared `docker-release.yml` reusable workflow on every push to main (tags: `main`, `sha-<short>`, `latest`; SBOM + provenance attestation; Trivy release gate), publishing `ghcr.io/damien-robotsix/robotsix-central-deploy`. The compose file now references that image (`docker compose pull` to update; `build: .` kept for local dev). Dockerfile: digest-pinned `python:3.14-slim` in both stages, uv brought in via `COPY --from=ghcr.io/astral-sh/uv` instead of `pip install uv`, runtime copies only site-packages + the console script, **runs as non-root (uid 1000)**, and declares a `HEALTHCHECK` on `/health`. ⚠ Existing deployments must chown the `central_deploy_data` volume once before running the non-root image (see docs/deployment.md).
- Align with the robotsix-standards repo baseline: add the MIT `LICENSE` file, switch the build backend from setuptools to hatchling (with `allow-direct-references` for the git-pinned first-party deps), link to [robotsix-standards](https://github.com/damien-robotsix/robotsix-standards) from README/AGENT.md, and add a `mkdocs build --strict` CI gate. Hatchling resolves the `ui/DEPLOY_CONTRACT.md` symlink when building the wheel (setuptools shipped a dangling link), so the Dockerfile now copies `docs/DEPLOY_CONTRACT.md` into the build stage and drops the post-install site-packages copy workaround. `docs/DEPLOY_CONTRACT.md` now declares itself the canonical home of the deploy contract (the copy in robotsix-standards had drifted and is now a pointer page).
- Dashboard onboard modal now polls `GET /onboard/jobs/{job_id}` after confirm, showing live deploy-phase progress (writing_config, deploying_primary, waiting_health, deploying_siblings, done, failed). The modal no longer waits on a single long request that could be dropped by nginx.
- Onboard form: mark secret fields as optional with clearer placeholder text and informational notes explaining the fill-later flow via Configure → Save.
- Fix ``AttributeError: 'State' object has no attribute 'job_registry'`` in lifecycle conftest fixture (the ``_reset_globals`` autouse fixture now initializes ``app.state.job_registry`` so validation-error tests on the onboard confirm endpoint don't crash before body validation).
- `POST /onboard/confirm` now returns `202 Accepted` with a job id instead of blocking for the full deploy. The long-running deploy sequence (primary deploy, health gate, sibling deploys) runs as an asyncio background task. Poll `GET /onboard/jobs/{job_id}` for phase progress (`writing_config` → `deploying_primary` / `waiting_health` → `deploying_siblings` → `done` / `failed`). A second confirm for the same component while a job is active returns `409 Conflict`.
- **Remove path-prefix gateway routing.** `deploy.robotsix.net/<name>/...` URLs are no longer proxied — path-prefix proxying broke any component app serving absolute asset URLs (e.g. `/static/…`). Components are now reached exclusively via their subdomain (`<name>.deploy.robotsix.net`, requires `gateway_base_domain`); legacy path URLs 307-redirect to the subdomain. WebSocket gateway connections are subdomain-only (non-subdomain hosts close with 4004). `http_proxy` loses its `prefix` param and the `x-forwarded-prefix` header / Location-rewrite logic.
- Record volume hash during config assist so drift detection works after auto-detected config changes.
- Add a `dependency-review` CI job (`actions/dependency-review-action`, `fail-on-severity: high`) that blocks PRs introducing dependencies with known high-severity vulnerabilities. Requires the repository's Dependency graph to be enabled (now on).
- Config drift UI: when a component's config has been edited out-of-band, the dashboard now shows a warning banner with Import/Edit-stale options, blocks blind Save with a conflict diff panel, and supports import-from-volume and explicit overwrite flows.
- `_rollback_onboard` now removes orphaned containers (primary + siblings) via `backend.remove_container` and cleans up any freshly-seeded `EnvStore` entry, preventing resource leaks when onboard deploy fails.
- Add inverse preflight gate: `/onboard/preflight` now returns 422 when a service declares `robotsix.deploy.config-target` but the repo yields no config schema (no `config/config.yaml`, `config/config.example.yaml`, or valid template). This prevents deploying containers with empty config volumes that would crash-loop.
- Document test-file organization rule: test files for module X belong under `tests/X/`, never at the `tests/` root
- Config drift detection: `ConfigYamlStore` now records a `volume_hash` after every write to a config volume. `GET /services/{name}/config` surfaces a `drift` flag when the live volume content diverges from the stored hash. `PUT /services/{name}/config` blocks blind overwrites on drift (HTTP 409) unless `force_overwrite: true` is passed. `POST /services/{name}/config/import` resyncs the store from the live volume, clearing drift.
- Add docstrings to all 16 public route handlers in `lifecycle/routers/services.py`, covering purpose, error responses, and side effects (sibling fan-out, store writes).
- PR #182 (docs: sync index and configuration pages with current code) — already merged; no further changes needed
- Add regression test confirming `TypeError` during port parsing is caught as `ParseError`. The existing bare-comma `except ValueError, TypeError:` syntax is correct Python 3.14+ under PEP 758 — it catches both exception types and is the `ruff format`-preferred style.
- Add `.pre-commit-config.yaml` with hooks for ruff, ruff-format, mypy, and common file checks (end-of-file, trailing-whitespace, merge-conflict, large-files)
- Add `docs/ARCHITECTURE.md` — comprehensive architecture guide covering system
  components, subpackage responsibilities, data flow, state machine, gateway
  routing rules, and key design decisions.
- Add `"volumes"`, `"login"`, and `"logout"` to `RESERVED_NAMES` in the gateway router to prevent onboarding components that would shadow central-deploy routes.
- Define `ExecutionBackendType(str, Enum)` in `lifecycle/models.py` with members `DOCKER_SDK`, `DOCKER`, `NOOP`, replacing bare `str` typing for the `execution_backend` config field. Update config, deps, cli, and all test fixtures to use the enum. Add grep-lint CI check for raw execution-backend strings outside `models.py`.
- Add `docker_sdk_timeout` config (default 120 s) to prevent indefinite blocking on Docker SDK operations like `images.pull()`; wired into `DockerSdkBackend` constructor and `LifecycleConfig` (env: `ROBOTSIX_LIFECYCLE_DOCKER_SDK_TIMEOUT`)
- Adopt `robotsix-yaml-config` for shared YAML primitives: replace local `_deep_merge` with `robotsix_yaml_config.deep_merge`, use `read_yaml_file` for file-based YAML reads in `store.py` and `registry/loader.py`, and add typed error handling (`YamlParseError`, `InvalidConfigStructureError`) at remaining `yaml.safe_load` sites in `backend.py` and `onboard/parser.py`.
- Add `.trivyignore` to suppress known CVEs in Debian base image system packages (perl, util-linux, tar, zlib1g, passwd, sysvinit-utils) that are not exploitable in the container's attack surface
- Dashboard Remove is now a modal instead of a chain of native `confirm()`
  dialogs. Data volumes are **preserved by default**; deleting them is an
  explicit, separately-warned opt-in checkbox (IRREVERSIBLE), and the confirm
  button changes to "⚠ Remove + DELETE data volumes" when checked. Backend
  behaviour (`DELETE /services/{name}?remove_volumes=`) is unchanged.
- Fix production image crash-loop: `ui/router.py` reads `DEPLOY_CONTRACT.md` at
  import time, but `src/robotsix_central_deploy/ui/DEPLOY_CONTRACT.md` is a
  symlink to the canonical `docs/DEPLOY_CONTRACT.md`, so the built wheel shipped
  a dangling link and the app died on startup with `FileNotFoundError`. The
  Dockerfile now copies the real `docs/DEPLOY_CONTRACT.md` into the installed
  package location. (Not caught by CI, which runs from the source tree where the
  symlink resolves.)
- Upgrade Debian system packages in Dockerfile base image to address CVEs in `python:3.14-slim` (perl, util-linux, tar, zlib1g, passwd, sysvinit-utils, and others).
- Register gateway module in docs/modules.yaml with its reverse-proxy endpoints, dependencies, and test suite.
- Add CodeQL SAST job to CI for taint-tracking vulnerability detection (security-extended and security-and-quality queries)
- Reverse DEPLOY_CONTRACT.md symlink direction: `docs/DEPLOY_CONTRACT.md` is now the canonical copy, `src/robotsix_central_deploy/ui/DEPLOY_CONTRACT.md` symlinks to it. CI guard updated accordingly.
- Extract private helpers from long route handlers in `lifecycle/routers/`:
  `_gather_sibling_health`, `_fanout_deploy_siblings`, `_fanout_rollback_siblings`,
  `_delete_component_volumes`, `_resolve_account_mode`, `_postprocess_config_assist`
  (services.py); `_deploy_onboard_siblings`, `_rollback_onboard` (onboard.py).
- Consolidate `volume_audit` as a sub-package of `lifecycle` (`src/robotsix_central_deploy/lifecycle/volume_audit/`) to reflect their strong coupling (shared config prefix, cross-imports).
- Enable the periodic security posture workflow to inspect CI workflows and pre-commit config against evolving OWASP/OpenSSF/SLSA best practices.
- Remove dead code: `is_active()` function and `ACTIVE_STATES` constant from `lifecycle.models` (neither had any callers).
- Remove stale `ROBOTSIX_LIFECYCLE_GHCR_TOKEN` documentation — the env var was never defined as a `LifecycleConfig` field, and `RegistryChecker` uses anonymous GHCR tokens fetched at runtime.
- Fix config-assist blanking un-submitted config: `POST /services/{name}/config/assist`
  submits only the seed fields the operator typed, but `_merge_config` reset every
  other key to the template default — so an operator's LLM api_key / observability
  config (any secret or section the assist form did not include) was silently wiped
  on each helper run. `_merge_config` gains a `prefer_existing_for_unset` flag (used
  only by the sparse config-assist path) so untouched keys keep their existing value.
  Repo-agnostic — no knowledge of any specific config key. The Save form (which
  renders every field) keeps the previous "unset → template default" behaviour.
- Define `HealthStatus`, `UpdateState`, `StoreBackend`, and `VolumeEntryType` `str, Enum` types in `lifecycle/models.py`; replace all duplicated hardcoded health/state/backend/volume-entry strings across `backend.py`, `deps.py`, `config.py`, `cli.py`, and `schemas.py` with canonical enum references. Add a CI `grep-lint` job to prevent raw-string backsliding.
- Add CI security scanning: `uv audit` for dependency vulnerabilities, ruff `S` (flake8-bandit) rules for SAST, Trivy container image scanning, and Gitleaks secret detection. Dockerfile converted to multi-stage to keep build-time tooling out of the runtime image.
- Add dedicated unit tests for the onboard fetcher module in ``tests/onboard/test_fetcher.py``, exercising real local git repos for clone-and-read integration logic.
- Add orphan-volume pruning: `GET /volumes/orphans` lists Docker volumes owned
  by no registered component and not attached to any container, and
  `POST /volumes/prune` removes them (IRREVERSIBLE). A component's own volumes
  (even when stopped) and in-use volumes are never pruned; the eligible set is
  recomputed server-side on every call. The dashboard gains an **Orphan Volumes**
  panel with a "Prune all" button (shown only when orphans exist).
- Fix missing re-exports in ``lifecycle/server.py`` backward-compat shim: add ``shutil``, ``NoopBackend``, and ``_fetch_fresh_config_assist`` so test monkeypatches resolve correctly after the modular split.
- Refactor monolithic `lifecycle/server.py` (2869 lines) into per-resource
  modules using FastAPI's `APIRouter` pattern:
  - `lifecycle/routers/health.py` — `/health`, `/disk`, `/disk/reclaim`
  - `lifecycle/routers/services.py` — all `/services/{name}/...` endpoints
  - `lifecycle/routers/volumes.py` — `/volumes/...` endpoints
  - `lifecycle/routers/onboard.py` — `/onboard/preflight`, `/onboard/confirm`
  - `lifecycle/schemas.py` — extracted Pydantic request/response models
  - `lifecycle/deps.py` — dependency factories, helpers, lifespan
  - `lifecycle/app.py` — FastAPI app assembly and router registration
  - `lifecycle/server.py` — backward-compatibility re-export shim
- Deduplicate `DEPLOY_CONTRACT.md`: keep canonical copy in `src/robotsix_central_deploy/ui/` (where the server reads it) and replace `docs/DEPLOY_CONTRACT.md` with a symlink. Trim `README.md` to a minimal overview — full content lives in mkdocs. Add CI guard against accidental copy drift. (mill: Deduplicate DEPLOY_CONTRACT.md — eliminate identical copy in docs/ and src/robotsix_central_deploy/ui/ (20260701T091044Z-deduplicate-deploy-contract-md-eliminate-8f52))
- Add `.yaml` extension to 6 periodic agent definition files (`audit`, `completeness_check`, `copy_paste`, `health`, `module_curator`, `test_gap`) so they are picked up by the periodic loader
- Fix 15 mypy errors and switch mypy to blocking mode in CI: add `types-PyYAML` and `types-docker` stubs, annotate bare `dict` types in `server.py`, add type annotations to `NoopBackend.run_config_assist` and `DockerBackend.run_config_assist`, add `[[tool.mypy.overrides]]` for docker, and set `mypy-advisory: false` so new type errors fail the build.
- Volume audit findings are now filed as board tickets when board API settings
  are configured (`ROBOTSIX_LIFECYCLE_BOARD_API_URL`, `ROBOTSIX_LIFECYCLE_BOARD_API_TOKEN`,
  `ROBOTSIX_LIFECYCLE_BOARD_REPO_ID`). The `robotsix-board-agent` library is now
  a project dependency.
- **Fix circular import in volume_audit**: moved `ExecutionBackend` and `LifecycleConfig`
  imports in `volume_audit/scheduler.py` under `TYPE_CHECKING` to break a circular import
  chain (`lifecycle` → `server` → `volume_audit.scheduler` → `lifecycle.backend`) that
  caused `ImportError` during test collection.

- Dashboard: primary row health badge now reflects overall component health (primary + siblings) with a per-container breakdown tooltip on hover.

- **Sibling health in component status**: `GET /services/{name}` now includes
  `sibling_health` (per-container health snapshots for sibling services) and
  `overall_health` (a component-level rollup that considers the primary plus
  all healthchecked siblings). Siblings without a Docker healthcheck are neutral
  and do not affect the rollup.

- **Volume browser**: added `GET /volumes/{name}/ls?path=` and
  `GET /volumes/{name}/cat?path=` backend endpoints plus a read-only
  volume-browser modal in the dashboard UI. Operators can click a volume
  in the disk panel, browse directories, and read text file contents
  (read-only, auth-protected, path-traversal safe).

- **Volume namespacing for onboarding**: `onboard_confirm` now prefixes all
  named-volume host names with the component name (e.g. `mail-auto-mail-config`
  instead of `auto-mail-config`) so two components from the same Docker image
  never share storage. `onboard_preflight` gains a volume-collision check (HTTP
  409) that prevents onboarding when the namespaced names would collide with an
  existing component's `named_volumes`.
- **Fix auto-detect add-account config corruption**: Fixed four bugs in `POST
  /services/{name}/config/assist` add_new mode that together corrupted existing
  accounts when adding a new one via auto-detect. The user-provided account name (or
  email-derived slug) is now always used as the new account's id, never a template
  placeholder like `<account-N>`. Existing account credentials are preserved
  verbatim in the pre-detect volume write so detect does not re-validate them. The
  post-detect merge no longer replaces the accounts list wholesale — existing
  accounts always come from storage. The seed bar's `accounts.0.*` overwrite no
  longer corrupts existing account slot 0 during re-merge. Invalid account ids are
  rejected with HTTP 422 before the detect command runs.
- **Reclaim build cache**: `POST /disk/reclaim` endpoint triggers Docker build-cache
  pruning via `ExecutionBackend.prune_builds()`. Returns `{"space_reclaimed_bytes": <int>}`.
  Noop and CLI backends return `0`; the SDK backend calls `docker builder prune` and
  reports the `SpaceReclaimed` value from the Docker API. Dashboard UI now includes a
  "Reclaim" button next to the "Build cache reclaimable" row on the disk panel.
- **Account name input in auto-detect**: The config-assist seed bar now shows an
  "Account name" text input when the component has account seeds. The value is sent as
  `account_name` in the request body and, when non-empty, overrides the email-derived
  account id slug in `add_new` mode. Removed automatic `default_account` normalization
  from the assist endpoint.
- **Centralized error handlers**: extracted inline `HTTPException` handler into
  `register_error_handlers()` in `lifecycle/error_handlers.py`. Added handlers for
  `RequestValidationError` (422 with structured `ErrorDetail` envelope) and a
  catch-all `Exception` handler (500 with safe, non-leaking response). All error
  responses now share the `{"error": ..., "detail": ...}` shape.
- **Settings disk warning threshold:** `disk_warn_bytes` renamed to `disk_warn_pct`
  (a float percentage, default 10.0%).  The Settings form now accepts "Disk Warning
  (% free)" with a `step=0.1` numeric input, and the `/disk` endpoint returns
  `warn_threshold_pct` instead of `warn_threshold_bytes`.  The disk panel displays a
  "Warn threshold" row and a dynamic banner like "⚠ Low disk space — free space is
  below X%!".  Existing `settings.json` files with the old `disk_warn_bytes` key
  silently drop it and use the 10.0% default.
- **Settings form secret-field placeholders**: `auth_password`
  now renders as `••• set — enter a new value to change` placeholder when configured
  (instead of disabled `***` in the value, which was indistinguishable from empty).
  Saving without clicking Change preserves the stored secret via `'***'` sentinel.
- **Disk warning threshold as percentage**: renamed `disk_warn_bytes` to
  `disk_warn_percent` (float, default 10.0) throughout the stack —
  `SystemSettings`, `LifecycleConfig`, settings API models, and the dashboard
  Settings form. `GET /disk` now computes `warn_threshold_bytes` as
  `int(disk_warn_percent / 100.0 * total_bytes)` at request time. The env var
  `ROBOTSIX_LIFECYCLE_DISK_WARN_BYTES` is replaced by
  `ROBOTSIX_LIFECYCLE_DISK_WARN_PERCENT`. Old `settings.json` files with
  `disk_warn_bytes` silently fall back to the 10% default.

- **Settings GET reflects env-var credentials**: `GET /settings` now reads
  from the effective config (env vars overlaid by stored settings) instead of
  the raw store, so env-var-supplied auth credentials appear in the UI even
  before the operator has saved settings via the UI.
- **Volume audit subsystem**: New optional `volume_audit` module that periodically
  scans Docker named volumes for size and growth-over-time tracking. Growth
  threshold breaches are reported through a pluggable `report_finding` seam
  (placeholder: persists to local JSON + logs WARNING). Configurable via
  `ROBOTSIX_LIFECYCLE_VOLUME_AUDIT_*` environment variables; disabled by default.
  New `GET /volumes/audit` endpoint returns live growth records.
- **Settings: mark GHCR Pull Token as optional** — label reads "GHCR Pull Token
  (optional)", placeholder text "Leave blank for public images", and help row
  "Only required for private GHCR registry images." to clarify it is not
  required for public images.
- **Dashboard panel for volume audit** — per-component volume sizes, growth
  deltas, and flagged findings shown under the Disk Usage section.

- **Fix: config-form Save corrupts multi-account configs** — five coordinated
  fixes for the `GET /config → form render → collectConfigValues → PUT /config`
  chain: (1) `_merge_config` dict-branch now recurses correctly when the
  destination key is absent from existing config, preventing `"***"` sentinel
  literals from being written to storage; (2) new `_prune_unset` post-merge
  pass removes template-default empty fields that were absent from existing,
  preventing resurrected `archive.namespace` etc.; (3) new
  `_validate_account_ids` server-side + client-side pre-fetch validation
  reject invalid account `id` slugs (e.g. email addresses with `@`) before
  they can crash-loop auto-mail; (4) `saveConfigValues` now calls
  `closeConfigModal()` on success instead of re-opening the modal; (5)
  array-item accordion headers prefer `itemCurrent.email` over `id`/`name`
  for human-readable account labels.
- **Account-aware config assist**: `POST /services/{name}/config/assist` now
  supports a `target_account_index` field to control whether auto-detect
  updates an existing account or adds a new one.  When omitted and accounts
  already exist, the new `add_new` mode injects a derived account ID and
  strips `--overwrite` from the detect command so existing accounts are not
  destroyed.  Backward-compatible: first-setup behaviour is unchanged.
- **Fix: auto-detect add-account corrupts existing account and leaks
  placeholders** — the seed bar in the config form collects values under
  `accounts.0.*`, but `add_new` mode rewrites placeholders to `accounts.N.*`.
  The server now relocates seed values from the template index (0) to the
  target slot so the volume seed write does not overwrite an existing account's
  username, `{accounts.N.*}` placeholders resolve to the submitted email rather
  than staying literal, and the account ID is derived from the email instead
  of falling back to `accounts-N`.
- **Fix: `_mask_secrets` no longer filters by secret-name heuristic** — removed
  the `_is_secret_name(key)` guard so that any leaf whose template value is
  `""` or `None` is treated as a secret and masked as `"***"`, matching the
  function's documented contract.
- **Dashboard: show per-component subdomain URLs** — the "↗ Open" link for each
  component now points to ``https://<name>.<gateway_base_domain>/`` instead of
  ``https://<gateway_base_domain>/<name>/``. Falls back to ``/<name>/`` when no
  ``gateway_base_domain`` is configured.
- **Gateway: add subdomain routing for component UIs** — each component is now
  reachable at ``<name>.<gateway_base_domain>/...`` (Host-header routing) so apps
  that embed absolute paths (e.g. ``/static/board.css``, ``/move``) work at root
  without the legacy ``/<name>/`` path prefix. The ``http_proxy`` Location-rewrite
  hack is now gated on ``prefix`` being non-empty (no rewrite for subdomain
  routing). The existing path-prefix fallback (``/<name>/...``) is preserved as
  a backward-compat path.

- **Config assist: persist detected config and normalise default_account** —
  `POST /services/{name}/config/assist` now persists the detected config to
  the `config_yaml_store` so that `GET /services/{name}/config` shows the
  detected values and clicking Save is idempotent (no longer clobbers working
  settings).  `_seed_for_detect` no longer writes template-default empty
  strings into the config volume, fixing blank config-assist forms.  When
  `merged["accounts"][0]["id"]` is set and `default_account` is absent/empty,
  `default_account` is normalised to that id.
- **Gateway: forward `x-forwarded-prefix`** — HTTP and WebSocket gateway
  proxies now set `x-forwarded-prefix: /{name}` so that upstream apps served
  under a subpath can construct correct absolute URLs.
- **Add MkDocs documentation infrastructure** — added `mkdocs.yml` with
  mkdocs-material theme and mkdocstrings[python] plugin for auto-generated
  API reference. New `docs/` dependency group in `pyproject.toml`
  (`uv sync --group docs`). `docs/index.md` converts the README into the
  MkDocs home page; `docs/api.md` stubs auto-generated API docs for all
  modules; `docs/changelog.md` includes `CHANGELOG.md` via pymdownx.snippets.
  `README.md` now points readers to the hosted docs site.
- **Document 16 missing lifecycle environment variables** — added
  `Auth`, `Docker`, `Disk`, `Registry`, `Logging`, `Gateway`, and
  `Claude Integration` sections to `docs/configuration.md`, and extended
  the `Persistence` table with store-path variables. All 22
  `ROBOTSIX_LIFECYCLE_*` env vars defined in `LifecycleConfig` are now
  documented.
- **Enable `env_doc_sync` periodic workflow** — added
  `.robotsix-mill/periodic/env_doc_sync.yaml` stub so that the
  `env_doc_sync` periodic workflow cross-references Pydantic Settings
  env vars against `docs/configuration.md` and files draft tickets for
  documentation gaps.
- **Config-assist API endpoint and dashboard UI** — added
  `POST /services/{name}/config/assist` endpoint that runs a component's
  repo-declared config-assist command in a one-shot container and returns
  auto-filled config values without persisting to the config store. Extended
  `GET /services/{name}/config` to return `config_assist_command` and
  `config_assist_seeds` fields. The config modal now shows an "Auto-detect /
  Assist" button and orange-bordered seed fields for components that declare
  the `robotsix.deploy.config-assist` label.
- **Config-assist seed-value placeholder substitution** — the config-assist
  command template now supports `{dotted.path}` placeholders that are
  substituted with the user's submitted seed values (navigated from the
  form body via dotted-path notation including list indices).  Detected
  output is deep-merged into the submitted config so the assist command
  never clobbers fields the user already entered.

- **Settings page: gateway base domain and Claude mount path** — added
  `gateway_base_domain` and `claude_host_mount_path` to `SystemSettings`,
  `LifecycleConfig`, the settings API (`GET`/`PUT /settings`), and the dashboard
  Settings UI. The `gateway_base_domain` is used at startup to build absolute
  `↗ Open` shortcut URLs for proxied services. The `claude_host_mount_path`
  replaces the hardcoded `~/.claude` default when set (requires service restart).

- **Populated `config/components.yaml`** — pinned all six components to non-`:latest`
  image tags (`:main` for robotsix services, `:3.3.0.0` for radicale), filled every
  `env` block with required environment keys, assigned non-conflicting host ports
  (8200–8202, 8300, 3000, 5232), confirmed named volumes and stateful-volume
  annotations, and removed all 18 `TODO` placeholder markers.

- **Configuration documentation** — added `docs/configuration.md` documenting
  all `ROBOTSIX_LIFECYCLE_*` environment variables (host, port, API key, store
  backend/path, execution backend).

- **Fleet alignment bootstrap** — added `.robotsix-mill/config.yaml`,
  `.github/workflows/ci.yml` (calling robotsix-mill reusable `python-ci.yml`),
  and `AGENT.md`. Added `[tool.mypy]` with `strict = true` and
  `[tool.coverage]` with `fail_under = 80` to `pyproject.toml`. Added
  `mypy`, `coverage`, `pytest-cov`, and `ruff` to dev dependencies.
  Fixed all `mypy --strict` type errors across the codebase.

- **Per-component config.yaml support** — central-deploy now fetches and parses
  `config/config.yaml` from onboarded repos at preflight time, persists the schema
  and user-saved values in `ConfigYamlStore`, exposes `GET`/`PUT /services/{name}/config`
  endpoints with secret masking, and writes merged YAML into a `{name}-config` Docker
  named volume at onboard and on every save. The deploy-contract docs are updated with
  the new `config/config.yaml` convention.

- **Config modal UI** — each component card now has a "Configure" button that opens
  an auto-generated form based on the config schema. Supports nested sections,
  secret fields with sentinel preservation (`***`), and safe refresh on save.

- **Fix OpenAPI/runtime mismatch in error responses** — the global
  `http_exception_handler` now returns `ErrorDetail` instances instead of raw dicts,
  ensuring runtime error bodies match the OpenAPI-declared `ErrorDetail` schema
  (`{"error": "...", "detail": "..."}`).

- **`DELETE /services/{name}` endpoint** — removes an onboarded component, its service
  records, environment variables, secrets, and in-memory registry entry. Supports an
  optional `?stop_container=false` query parameter to skip container stop/removal
  (default: `true`). Container stop and remove are best-effort — errors are logged at
  `WARNING` and do not abort the deletion. Sibling services are also stopped, removed,
  and cleaned up.

- **Calendar stack contract-conforming compose files** — authored
  `# central-deploy-contract-version: 1` compose files for `robotsix-calendar-agent`
  and `robotsix-radicale` and pushed them to their respective repos.  The calendar-agent
  compose removes the `build:` block, `restart:`, `extra_hosts:`, and watchtower labels;
  converts env vars to the contract `KEY=default` / `KEY=` syntax; and adds a healthcheck
  matching the Dockerfile (`CMD python /app/healthcheck.py`).  The radicale repo (newly
  created) wraps `tomsquest/docker-radicale` with a `radicale-data` named volume carrying
  the `robotsix.deploy.stateful` label.

- Updated `config/components.yaml` baseline entries for `calendar-agent` and `radicale`
  with correct image refs, env keys, healthcheck commands, and the `stateful_volumes`
  annotation for `radicale-data`.

- **Dashboard Remove button** — added a per-row "Remove" button to the component
  dashboard that calls `DELETE /services/{name}?stop_container=<bool>`. Two
  `window.confirm` prompts guard against accidental removal: the first selects
  whether to also stop/remove the container, the second is a final confirmation.

- **Dashboard env/secrets config modal** — added a per-component "Config" button
  that opens an env/secrets editing modal.  Users can view, add, edit, and delete
  environment variables and secrets from the UI without SSH.  The modal uses the
  existing `GET/PUT/DELETE /services/{name}/env` API endpoints and matches the
  dashboard's dark theme styling.

- **Parse `container_name` from compose YAML** — `parse_compose()` now reads
  `container_name` from the service definition and passes it through `DerivedSpec`.
  `onboard_confirm` uses `spec.container_name or spec.name` to set the container name
  on both `ComponentConfig` and `ServiceRecord`.  Empty string defaults to the component
  name, matching the existing broker (agent-comm) behaviour.

- **Encrypted env secrets storage** — new `SecretKeyManager` (Fernet key generation
  and encrypt/decrypt) and `EnvStore` (JSON persistence for per-component env overrides
  and encrypted secret tokens).  Three new API endpoints:
  `GET /services/{name}/env` (returns plaintext env + masked secrets),
  `PUT /services/{name}/env` (upsert env values and secrets),
  `DELETE /services/{name}/env/{key}` (remove a key from env or secrets).
  Merged env (base YAML + user overrides + decrypted secrets) is injected into
  container creation at `deploy_service()` and `rollback_service()` time.
  New config fields: `env_store_path` (`ROBOTSIX_LIFECYCLE_ENV_STORE_PATH`,
  default `component_env.json`) and `secret_key_path`
  (`ROBOTSIX_LIFECYCLE_SECRET_KEY_PATH`, default `secrets.key`).
  Added `cryptography>=41.0` dependency.

- **Onboard UI modal** — "Add Component" button on the dashboard opens a two-step
  onboard modal: enter git URL + name, fetch+review the derived spec (ports, volumes,
  env, Claude mount), edit secrets, and deploy.  Stateful volumes show an amber
  "starts EMPTY" warning.  Validation errors and 409 conflicts are surfaced inline.
- **Onboard API endpoints** — new `POST /onboard/preflight` (fetch+validate a service
  repo's compose) and `POST /onboard/confirm` (persist config, deploy container,
  register component).  Dynamic `ComponentConfigStore` persists onboarded components
  to JSON and re-loads them on server restart.  `DockerSdkBackend` pre-creates named
  volumes before container creation and injects an optional `~/.claude` host mount.

- **Volume error handling** — named volume creation in `DockerSdkBackend.deploy()`
  is wrapped in try/except with clear error messages for invalid volume names and
  Docker daemon connectivity issues.  Volume-already-exists (HTTP 409) errors are
  handled gracefully with a log message.
  Added `claude_mount`, `named_volumes`, and `stateful_volumes` fields to
  `ComponentConfig`.
- **Onboard-from-git compose parser** — new `onboard` package with
  `fetch_compose_bytes` (shallow git clone + raw bytes) and
  `parse_compose` (validates against the v1 deploy contract and
  returns a `DerivedSpec`).  Contract covers: single-service, no-build,
  named-volume-only, env key extraction, healthcheck Go-duration
  conversion, and `robotsix.deploy.*` extension labels (claude-mount,
  stateful-volume flagging).
  - `docs/deploy-contract.md` updated: missing header = parse error,
    image accepts any non-empty string, env allows preset defaults,
    driver enforcement removed.
- **Enable `VOLUMES=1` on socket-proxy** — named-volume create/list/inspect/remove
  calls now pass through the Docker socket proxy, in preparation for
  managed-service volume creation in the self-service deploy flow.
- **Dashboard "Up to date" column** — the live dashboard now displays an
  "Up to date" column showing each component's update state as a colored
  badge (green "up to date", amber "update available" with digest tooltip,
  grey "unknown").  Column sits between Health and Actions.
- **Registry image-update detection** — new `registry_check` subpackage with
  `RegistryChecker` that polls the GHCR registry for the latest manifest
  digest and compares against the deployed image digest.
  - `GET /services/{name}` now returns `update_available` (bool),
    `running_digest`, and `latest_digest` fields.
  - `GET /services` list items include `update_available`.
  - New config options: `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_TTL` (cache seconds, default 300),
    `ROBOTSIX_LIFECYCLE_REGISTRY_CHECK_INTERVAL` (background poll seconds;
    0 disables).
  - `deployed_image_digest` now stores the manifest digest (from
    `RepoDigests`) instead of the image config digest, making it
    comparable to the registry's `Docker-Content-Digest` header.
  - Background task updates all service records periodically when
    `registry_check_interval > 0`; shuts down cleanly on server exit.

- **Added `follow` query param to logs endpoint** — `GET /services/{name}/logs`
  now accepts `follow=bool` (default `false`).  When `follow=true`, the
  `DockerSdkBackend` passes `follow=True` to the Docker SDK and iterates log
  chunks via `run_in_executor` instead of a bare synchronous `for` loop,
  preventing event-loop blocking.  `NoopBackend` and `DockerBackend` stubs
  accept the new parameter.  Client disconnects trigger `log_iter.close()` via
  `asyncio.CancelledError` handling.

- **Removed legacy `auth_username`/`auth_password` fields** from
  `LifecycleConfig` — these env vars were no longer consulted by
  `verify_auth`, which matches passwords against `api_key` alone.
- **Removed dead backward-compat alias** — `verify_api_key = verify_auth`
  alias removed from `src/robotsix_central_deploy/lifecycle/auth.py`.  All
  callers already use `verify_auth` directly.
- **Monitoring UI dashboard** — new `GET /ui` endpoint serves a self-contained
  HTML dashboard at `/ui` showing live component status, image revision (first 12
  chars), health, and start/stop/restart controls.  Auth-gated via `verify_auth`;
  auto-refreshes every 30 s.  Logs column placeholder present for future
  logs-viewer ticket.  No external CDN dependencies (vanilla JS + CSS).
- **Log viewer modal** — clicking "Logs" for any component opens a dark-themed
  modal that streams container logs via `fetch` + `ReadableStream` (no page
  reload).  Shows last 200 lines then live-follows new output.  Close via × button
  or Escape key aborts the fetch cleanly.  Auth credentials included
  (`credentials: 'same-origin'`); 401 errors surface in the status bar.
- **Unified auth** — `verify_auth` now accepts `Authorization: Basic` where the
  password equals `ROBOTSIX_LIFECYCLE_API_KEY` (username is ignored).  Added
  `verify_api_key = verify_auth` alias for backward compatibility.  New private
  helper `_decode_basic_auth`.  Realm changed to `"Robotsix Central Deploy"`.
  Existing `X-API-Key` header path unchanged.

- **Docker socket proxy** — added `docker_socket_url` config field (`ROBOTSIX_LIFECYCLE_DOCKER_SOCKET_URL`, default `unix:///var/run/docker.sock`) and a `docker-compose.yml` with `tecnativa/docker-socket-proxy` sidecar scoped to `CONTAINERS`, `POST`, `DELETE`, and `IMAGES` API paths. Raw socket mounted read-only into proxy only; central-deploy talks TCP.
- **Logs streaming endpoint** — `GET /services/{name}/logs` returns container
  logs as `text/plain` (auth-gated).  Supports `tail` (1–10000, default 100)
  and `since` (ISO 8601 or Unix timestamp) query parameters.  Implemented for
  `DockerSdkBackend` (real logs via Docker SDK), `NoopBackend` (stub), and
  `DockerBackend` (CLI stub).
- **HTTP Basic Auth support** — `verify_auth` dependency now accepts either
  `X-API-Key` header or `Authorization: Basic` credentials. New config fields
  `auth_username` / `auth_password` (env: `ROBOTSIX_LIFECYCLE_AUTH_USERNAME`,
  `ROBOTSIX_LIFECYCLE_AUTH_PASSWORD`). Auth failures return `401` with
  `WWW-Authenticate: Basic realm="Central Deploy"` to trigger browser login
  dialogs. `GET /services` and `GET /services/{name}` are now authenticated
  when credentials are configured. `GET /health` remains open.
- **Component registry** — Pydantic models (`ComponentConfig`, `PortMapping`,
  `VolumeMount`, `HealthCheck`) and a YAML loader (`ComponentRegistry.from_yaml`)
  that declares every managed Docker component in a single source of truth.
- **Seed configuration** (`config/components.yaml`) — stub entries for the six
  current server services: cost-monitor, calendar-agent, auto-mail, chat, broker
  (agent-comm), and radicale.
- **Lifecycle config** — added `registry_path` field and `effective_registry_path`
  property to `LifecycleConfig`, overridable via `ROBOTSIX_LIFECYCLE_REGISTRY_PATH`.

- **Docker SDK backend** — new `DockerSdkBackend` talks to the Docker daemon
  directly via the Python Docker SDK (no CLI subprocess).  `status()` returns a
  `ComponentInspect` with image revision and health; `start`/`stop`/`restart`
  use the SDK's container API.  The abstract `ExecutionBackend.status()` now
  returns `ComponentInspect` and the old `DockerBackend` was updated to match.
- Enable periodic analysis workflows: audit, health, test_gap,
  module_curator, completeness_check, copy_paste, state_sync.

## [0.1.0] — 2025-06-25

### Added

- **Lifecycle control API** — REST server for managing suite services.
  - `POST /services/{name}/start` — start a service (idempotent).
  - `POST /services/{name}/stop` — stop a service (idempotent).
  - `POST /services/{name}/restart` — restart a service (idempotent).
  - `GET /services/{name}` — full status for one service.
  - `GET /services` — list all managed services with current state.
  - `GET /health` — liveness probe.
- **Service state machine** — seven states (stopped, starting, running,
  stopping, restarting, failed, unknown) with formal transition rules and
  idempotent operations.
- **Pluggable execution backend** — abstract `ExecutionBackend` with a
  `DockerBackend` (subprocess-driven) and a `NoopBackend` for testing.
- **Persistence layer** — `InMemoryStore` (ephemeral) and `FileStore`
  (YAML-backed, survives restarts).
- **Bearer-token auth guard** — `X-API-Key` header validated against
  `ROBOTSIX_LIFECYCLE_API_KEY`; mutating endpoints are gated, dev mode
  when no key is set.
- **CLI entrypoint** — `robotsix-lifecycle` with `--host`, `--port`,
  `--store-backend`, `--execution-backend`, `--api-key` flags.
- **Test suite** — 78 tests covering models, state machine, store
  implementations, backend, server endpoints, and auth.
- **OpenAPI 3.0 docs** — auto-generated at `/docs` and `/openapi.json`.
