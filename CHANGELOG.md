# Changelog

All notable changes to robotsix-central-deploy.

<!-- towncrier release notes start -->

## 0.0.0 (unreleased)

- Add `mill` to the chat-agent service allowlist, permitting the chat agent to restart and update the mill service via the scoped `/chat/services/mill/restart` and `/chat/services/mill/update` endpoints.
- Add docstring to ``CaretakerScheduler.__init__`` constructor documenting all 11 dependency-injection parameters.
- Add generic credential-sharing mechanism: scope-tag env vars and secrets in any component's settings, then declare `consumed_scopes` patterns on consumer components. On deploy, the server resolves matching credentials across all EnvStores and injects them into the consumer's container — no manual key duplication needed.
- Added docstring to `gateway_http` in `src/robotsix_central_deploy/gateway/router.py` documenting the two routing strategies (subdomain and legacy path-prefix redirect).
- Added docstring to `RateLimitMiddleware.dispatch` documenting the two-tier rate limiting logic (login POST with lockout, API paths with per-hour limit, and gateway exemption).)
- Enable `docstring_coverage` periodic workflow for automated docstring gap detection.
- **Chat config secrets support**: `PUT /chat/config/{name}` now accepts secret (`writeOnly`/`password`) fields with partial-update semantics — omitted or sentinel values keep the stored secret, only explicitly supplied values overwrite. Added `GET /chat/config/{name}` for reading config with secrets redacted. Rollback preserves current secret values, audit log redacts secret data, and config volume file permissions tightened to 0600.
- Configure panel: render schema docstrings as inline markdown (code, bold, italic, links), collapse long section descriptions with a more/less toggle, show per-field help captions under each input, and group top-level scalar keys under a "General" section.
- ops: Remove stale ungrouped Dependabot entries that were left on disk alongside the new grouped entries, reducing total entries from 8 to 4
- Add docstrings to the five undocumented route handlers in the UI router (dashboard, login_page, login_submit, logout, get_deploy_contract).
- Enable `survey` periodic agent for discovering and studying similar deployment/orchestration projects
- Fix caretaker mill ingest 422 validation error: send ``source_tag`` (required by mill's TicketIngest schema) instead of unrecognised ``kind`` field. Also log response body on non-2xx ingest responses so future rejections are diagnosable.
- Deploy 409 responses now include lock-holder metadata (source, started-at, job-id), and the dashboard surfaces it with the source and start time instead of a bare "already in progress" message.
- Add `secure_headers` module to the Lifecycle Internals section of the API reference docs.
- Add PR-review endpoints to the chat-facing GitHub proxy: list reviews (`GET .../pulls/{number}/reviews`), list inline review comments (`GET .../pulls/{number}/comments`), submit a review (`POST .../pulls/{number}/reviews` with APPROVE/REQUEST_CHANGES/COMMENT), and dismiss a review (`PUT .../pulls/{number}/reviews/{review_id}/dismissals`). Review submission falls back to the repo-creation PAT when the App identity is the PR author (GitHub rejects self-approval). The PR detail endpoint also now returns `mergeable_state` and `head_sha`. All endpoints use the existing GitHub App installation token minted server-side — no credential is exposed to the chat container. Chat-skill document updated accordingly.
- Enable periodic `state_sync` agent (`.robotsix-mill/periodic/state_sync.yaml`) for cross-referencing state Enum members against string-literal reference sites
- Consolidate `volume_audit` module into `caretaker/volume_audit/` as a sub-package, since it is exclusively consumed by caretaker. Move source from `src/robotsix_central_deploy/volume_audit/` to `src/robotsix_central_deploy/caretaker/volume_audit/`, tests from `tests/volume_audit/` to `tests/caretaker/volume_audit/`, and docs from `docs/volume_audit/` to `docs/caretaker/volume_audit/`. Update all imports, module registration, and documentation references.
- Parent Update button now cascades to all sub-component siblings (children), so the whole component group converges to a consistent revision set. The parent's "Up to date" badge aggregates the group — if any child has an update available the parent shows "update available (child)" instead of a misleading green badge. The ``POST /chat/services/{name}/update`` API endpoint also fans out to siblings.
- Remove Log Level from the operator-facing settings UI. The chat agent can still raise or lower the root logger level via `PUT /chat/config/{name}` by submitting `{"log_level": "DEBUG"}` (or any valid level), providing the diagnostic verbosity control it needs during troubleshooting.
- **Langfuse proxy:** restructured chat-accessible endpoints from a generic `GET /chat/langfuse/api/public/{path:path}?project=X` to structured routes with the project as a path parameter. Added `GET /chat/langfuse/projects` (list configured project aliases), and added `robotsix-mill` project support alongside existing `robotsix-chat` and `cognee` projects. The `limit` query parameter is now capped at 100 server-side, and unknown project aliases return 404 while unconfigured keys return 503.
- Fix hadolint DL3008 inline ignore comments in Dockerfile: strip trailing
  text after rule name so hadolint v2.9.2+ correctly suppresses the warnings.
- Fix hadolint DL3008 ignore directives in Dockerfile: hadolint requires a `#` separator before explanatory text after the rule name (`# hadolint ignore=DL3008 # explanation`), not an em dash.
- Fix hadolint DL3008 suppression by merging two-line ignore comments into single lines in Dockerfile
- Pin git version in apt-get install commands in Dockerfile to satisfy hadolint DL3008
- Add Hadolint Dockerfile linting (`.hadolint.yaml` config, CI job with SARIF upload, pre-commit hook)
- Extract duplicated `CLAUDE_AUTH_VOLUME` constant into `lifecycle/models.py` to eliminate three-site synchronization fragility.
- Extract shared settings defaults into `lifecycle/_settings_defaults.py` so `SystemSettings` and `LifecycleConfig` share a single source of truth for their 20 common field defaults, eliminating the synchronization fragility that caused a 2026-07-05 production bug when `rate_limit_api_per_hour` defaults drifted apart. The `overlay()` method now derives its field set from the same shared keys.
- Reorganize lifecycle documentation into `docs/lifecycle/`: move `api.md`, `configuration.md`, and `openapi.json` from the `docs/` root into a dedicated per-module directory, matching the layout pattern of all other modules.
- Extract inline HTML from `get_deploy_contract()` into a Jinja2 template (`ui/templates/deploy-contract.html`), using `_escape_html()` for pre-escaped content rendering.
- refresh-schema regression test: current config values survive
  POST /services/{name}/config/refresh-schema byte-for-byte.
- Fix `.hidden` CSS class conflict with JavaScript `style.display` manipulation in dashboard; replace `style.display` toggles with `classList` operations for all elements using the `hidden` class
  Extract remaining inline `style=` attributes from JS-generated HTML strings in `dashboard.js` into CSS classes in `dashboard.css`
- Extract inline CSS from `login.html` into `static/login.css`
- Deploy no longer silently overwrites a drifted config volume with stale
  stored defaults.  When the live volume hash differs from the stored
  ``volume_hash`` at deploy time, the server auto-imports the live volume
  as current (with a warning log) and proceeds, preserving operator edits.
- Add `ServiceConfig` and `ConfigAssistSeed` to `registry/__init__.py` public API surface, and update internal consumers to import from the registry package rather than directly from `registry.models`.
- Enable `completeness_check` periodic agent to surface wiring gaps across backends, abstract methods, and config consumers.
- Add `src/robotsix_central_deploy/lifecycle/**/*` and `tests/lifecycle/**/*` globs to the lifecycle module in `docs/modules.yaml`, covering previously unclaimed source and test files.
- Add `paths:` declaration to the `ui` module in `docs/modules.yaml` covering source, tests, and docs directories
- Add `paths:` block to the `caretaker` module entry in `docs/modules.yaml`, covering source, tests, and docs.
- Remove unused `cookie_httponly` parameter from `GatewayAwareCSRFMiddleware` — it was accepted for backward compatibility but never forwarded to the underlying `asgi_csrf` call and had no callers.
- Enable `module_curator` periodic agent to keep `docs/modules.yaml` in sync with the live directory tree.
- Extract shared `_call_github_endpoint` helper in `chat_github.py` to eliminate duplicated try/except/raise boilerplate across 9 GitHub endpoint handlers. Read handlers use the helper directly; write handlers additionally pass an `audit_entry` for audit logging on success.
- Extract shared `_read_and_parse_credentials` helper in `_auth_ops.py` to eliminate duplicated volume-exist → read → parse orchestration in `check_claude_auth` and `read_claude_credentials`.
- Added `GET /chat/github/repos/{owner}/{repo}/actions/permissions/workflow` and `PUT` endpoints to read and set default workflow permissions (including `can_approve_pull_request_reviews`).
- Extended `PATCH /chat/github/repos/{owner}/{repo}` to accept `allow_auto_merge` and `delete_branch_on_merge`, and reject unknown keys with 422.
- Rate-limit deploy-job and onboard-job poll intervals from 1.5 s to 5 s to reduce 404 noise when the server restarts and loses in-memory job state.
- Add `POST /chat/github/repos/{owner}/{repo}/pulls/{number}/merge` endpoint for merging (or merge-queuing) pull requests via the GitHub App installation token. Optional `merge_method` and `sha` guard are passed through to GitHub. When the repository requires a merge queue, the endpoint falls back to a raw API requester to enqueue the PR. Returns 404 for repos the credential doesn't cover, 405 if merge is not allowed, 409 on conflicts, 422 for GitHub-side rejections, and 503 when the App is not configured. The github component skill doc now includes the endpoint with an explicit 🛑 confirmation-gate safety rule.
- Replace all inline `onclick=` handlers in the dashboard UI with
  delegated `data-action` attributes so the strict CSP
  (`script-src 'self'; script-src-attr 'none'`) works correctly.
  Move the login page's inline `<script>` block into a static
  `login.js` file.  Add regression tests that assert no
  `onclick=` or inline `<script>` remains.
- Extract duplicated volume-write boilerplate from ``write_config_to_volume`` and ``write_llmio_tier_config_to_volume`` into private ``_write_json_to_volume`` helper
- Replace unmaintained `starlette-csrf` with actively maintained `asgi-csrf` (v0.11) for CSRF protection. The `GatewayAwareCSRFMiddleware` pattern (skipping CSRF for gateway-proxied subdomain requests) is preserved.
- Extract ``_read_volume_credentials`` helper in ``_auth_ops.py``, deduplicating the busybox container-run boilerplate shared between ``check_claude_auth`` and ``read_claude_credentials``.
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
