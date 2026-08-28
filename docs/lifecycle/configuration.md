# Configuration

central-deploy follows the fleet [config standard][config-standard]: **one
pydantic model, one JSON file, no environment overlay.**

- The model is `LifecycleConfig` in `lifecycle/config.py` — 54 fields, each
  with a type, a default, and a description.
- The file is `config/config.json`, located by the single environment variable
  **`ROBOTSIX_CONFIG_FILE`**. That variable only *locates* the file; it never
  carries a value. In the deployed stack the compose file points it at
  `/data/config.json`.
- `config/config.schema.json` is the model reflected as JSON Schema, committed
  and kept in sync by the **Config Schema Drift** CI job. The deploy UI reads
  it to render typed inputs and the per-field help bubbles.

`LifecycleConfig` is a plain `BaseModel`, not `BaseSettings`, and nothing in
`src/` reads `os.environ`. There is no `ROBOTSIX_LIFECYCLE_*` variable, and
setting one has no effect.

## Where the field reference lives

**In the model, and nowhere else.** Every field's type, default, allowed
values, and description are on `LifecycleConfig`; `config/config.schema.json`
is the generated reflection of exactly that, and the deploy UI renders it.

This page deliberately does **not** restate them. It used to, as a hand-written
table of 24 environment variables — which by the time it was removed named a
mechanism that no longer existed, documented four settings nothing read, and
omitted 22 fields that did exist. A second copy of the schema is a second thing
to forget to update.

To read the fields:

```bash
# the model — descriptions live next to the defaults
$EDITOR src/robotsix_central_deploy/lifecycle/config.py

# or the generated schema
python -m json.tool config/config.schema.json
```

## The three layers, in order

A field's effective value comes from the last layer that sets it.

1. **`config/config.json`** — read at startup by `robotsix_config.load_config`.
   Anything the file omits falls back to the model's field default; a missing
   file means "all defaults".
2. **Self-contract labels** — on first boot, `robotsix.deploy.settings.*`
   labels on central-deploy's own deploy contract
   (`self_contract_path`, default `deploy/docker-compose.yml`) seed the
   settings store. This is how a fresh deployment arrives with its operator
   settings already set.
3. **`data/system_settings.json`** — the operator-editable store behind
   `GET`/`PUT /settings` and the dashboard's Settings panel, applied over the
   config by `SystemSettingsStore.overlay`. Changes take effect without
   editing `config.json` and without a redeploy.

Only the keys in `SETTINGS_DEFAULTS` (`lifecycle/_settings_defaults.py`) take
part in layer 3 — 18 of the 54 fields:

`caretaker_enabled`, `caretaker_interval_hours`,
`chat_agent_registration_enabled`, `claude_auth_refresh_interval`,
`disk_warn_pct`, `gateway_base_domain`, `ghcr_pull_token`, `image_auto_prune`,
`llmio_tier_config`, `log_level`, `mill_component_id`, `mobile_token_ttl_days`,
`rate_limit_api_per_hour`, `registry_check_interval`, `volume_audit_enabled`,
`volume_audit_growth_threshold_pct`, `volume_audit_interval_seconds`,
`volume_audit_min_delta_bytes`.

Adding a key to `SETTINGS_DEFAULTS` adds it to the overlay automatically —
`SystemSettings` and `LifecycleConfig` both source their defaults from it, so
the two cannot drift apart.

!!! warning "The overlay is silent, and it wins"
    A value in `system_settings.json` overrides `config.json` without
    appearing there. A revoked `ghcr_pull_token` stored in the settings file
    sat shadowing a working GitHub App credential for 15 days and blocked
    every fleet image pull. The startup log now names which source each
    `ghcr.io` credential came from — grep for `ghcr.io:` when a pull 403s.

One exception keeps the overlay honest: a **stored value equal to its default
does not override** a differing config value. A default cannot express an
override, only the absence of one. To force a key back to its default, clear it
in `config.json` rather than storing the default.

## Auth

central-deploy ships **no** authentication of its own — no login page, no
session store, no API key, no HTTP Basic credentials. The fleet edge
(Traefik + tinyauth) is the only gate; see [The edge](../edge.md).
`verify_auth` on the JSON API is a deliberate no-op stub, kept only as an
interception point, and `tests/lifecycle/test_app.py` fails if any route ever
grows a real credential dependency.

## Rate limiting

`rate_limit_api_per_hour` is the **only** rate limit central-deploy enforces.
`RateLimitMiddleware` (`lifecycle/rate_limiter.py`) applies it per client IP to
central-deploy's own JSON API (`/services`, `/volumes`, `/onboard`, `/chat`, …)
and returns HTTP 429 above the limit. Component traffic is served by the
Traefik edge and never reaches this middleware.

The default is deliberately high (20000/hour): the dashboard polls several
endpoints every few seconds, so a single open tab costs roughly 5000 requests
an hour from one IP.

There are no login rate limits, because there is no login.

## What `environment:` is still for

Per [§5 of the config standard][config-standard-env], compose `environment:`
carries deploy-topology wiring, never first-party settings. In this stack that
means `ROBOTSIX_CONFIG_FILE` on central-deploy itself, and the scope flags on
the `socket-proxy` sidecar — a third-party image that takes its configuration
however it takes it.

The per-component `EnvStore` slots central-deploy manages for *other* services
follow the same rule: they exist for third-party images, not for first-party
components, which carry their settings in their own `config.json`.

[config-standard]: https://damien-robotsix.github.io/robotsix-standards/config-standard/
[config-standard-env]: https://damien-robotsix.github.io/robotsix-standards/config-standard/#5-what-environment-is-for
