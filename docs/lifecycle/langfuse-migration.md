# Langfuse Credential Migration

How to move Langfuse project credentials from legacy storage (deploy-plane
environment variables, hand-aggregated lists) into each component's own
standardized config — the canonical home defined by the
[config-ownership standard](
  https://damien-robotsix.github.io/robotsix-standards/config-ownership/
).

## Background

Before this migration, Langfuse credentials lived in two places, both
outside the components that own them:

- The **chat agent** (`robotsix-chat`) read `LANGFUSE_PUBLIC_KEY`,
  `LANGFUSE_SECRET_KEY`, `LANGFUSE_COGNEE_PUBLIC_KEY`, and
  `LANGFUSE_COGNEE_SECRET_KEY` from its own `EnvStore`.  Every Langfuse
  project the chat agent needed was a separate env-var pair, and the
  deploy plane had no visibility into which projects existed.

- The **cost monitor** (`robotsix-cost-monitor`) maintained a
  hand-aggregated `projects[]` list in its own config, duplicating
  credentials the chat agent also held.

After the migration each component declares its own Langfuse projects in
its standardized config under a single `langfuse` block:

```json
{
  "langfuse": {
    "host": "https://langfuse.robotsix.net",
    "projects": {
      "<project-alias>": {
        "public_key": "pk-lf-...",
        "secret_key": "sk-lf-...",
        "project_id": "cm..."
      }
    }
  }
}
```

The central-deploy control plane auto-discovers every project and serves
them through two endpoints:

| Endpoint | Consumer |
|---|---|
| `GET /fleet/langfuse` | cost monitor — every project across the fleet |
| `GET /chat/langfuse/projects` | chat agent — proxy-authenticated Langfuse access |

There is **no fallback** to deploy-plane environment variables.  A
component whose credentials have not been migrated reports no projects,
and the chat agent and cost monitor receive no credentials for it.
This is the intended, visible failure — it signals that migration is
still in progress.

## Component → project ownership

Every component that generates LLM traces declares one or more Langfuse
projects.  The project alias follows the convention `<repo>` for the
component's main function and `<repo>-<function>` for additional
subsystems:

| Component | Project aliases |
|---|---|
| `robotsix-chat` | `robotsix-chat` (default), `cognee` |
| `robotsix-mill` | `robotsix-mill` |
| *(other components)* | `<component-id>` |

A component with two tracing functions declares two entries, never one
shared project.

## Prerequisites

- The central-deploy server is running a version that includes the
  auto-discovery and fleet-endpoint changes (children 1 and 2 of the
  Langfuse ownership epic).
- You have access to each component's settings UI.
- You have the plaintext Langfuse credentials for every project.

## Migration steps

### 1. Verify the fleet endpoint is working

```bash
curl -sH "X-API-Key: $DEPLOY_API_KEY" \
  https://deploy.robotsix.net/fleet/langfuse | jq .
```

The response lists every component and its declared projects.  Before
migration it will be empty (no component has credentials yet) except
for any operator-configured overrides in `LifecycleConfig.langfuse_projects`.

### 2. Enter credentials in each component's config

For each component in the ownership table above:

1. Open the component's settings UI, or use its `/config` API directly
   through the gateway:

   ```bash
   # Export the current config
   curl -sH "X-API-Key: $DEPLOY_API_KEY" \
     "https://deploy.robotsix.net/services/<component-id>/config/export" \
     | jq . > /tmp/cfg.json
   ```

2. Add the `langfuse` block.  Example for `robotsix-chat`:

   ```json
   {
     "langfuse": {
       "host": "https://langfuse.robotsix.net",
       "projects": {
         "robotsix-chat": {
           "public_key": "pk-lf-abc123...",
           "secret_key": "sk-lf-abc123...",
           "project_id": "cmpuy5kkr0006nt062uqlh5qo"
         },
         "cognee": {
           "public_key": "pk-lf-def456...",
           "secret_key": "sk-lf-def456...",
           "project_id": "cm..."
         }
       }
     }
   }
   ```

3. Import the updated config:

   ```bash
   curl -sH "X-API-Key: $DEPLOY_API_KEY" \
     -X PUT "https://deploy.robotsix.net/services/<component-id>/config" \
     -H "Content-Type: application/json" \
     -d @/tmp/cfg.json
   ```

   Or use the component's own `/config` endpoint through the gateway:

   ```bash
   curl -sH "X-API-Key: $DEPLOY_API_KEY" \
     -X PUT "https://<component-id>.deploy.robotsix.net/config" \
     -H "Content-Type: application/json" \
     -d @/tmp/cfg.json
   ```

4. Repeat for every component.

**Security note:** Enter plaintext credential values through the
settings UI or API — never paste them into tickets, code, or chat
messages.

### 3. Verify auto-discovery

After entering credentials for all components, the fleet endpoint
should list every project:

```bash
curl -sH "X-API-Key: $DEPLOY_API_KEY" \
  https://deploy.robotsix.net/fleet/langfuse | jq '.components[].projects[].alias'
```

Expected output (example):

```
"robotsix-chat"
"cognee"
"robotsix-mill"
```

Also verify the chat-agent proxy:

```bash
curl -sH "X-API-Key: $DEPLOY_API_KEY" \
  https://deploy.robotsix.net/chat/langfuse/projects | jq 'keys'
```

### 4. Verify consumers

**Cost monitor:**

```bash
curl -sH "X-API-Key: $DEPLOY_API_KEY" \
  https://cost-monitor.deploy.robotsix.net/api/projects | jq .
```

All expected project aliases should appear.

**Chat agent:**

Send a test trace-proxy request through the chat-agent Langfuse proxy:

```bash
curl -sH "X-API-Key: $DEPLOY_API_KEY" \
  https://deploy.robotsix.net/chat/langfuse/robotsix-chat/traces | jq .
```

## Cleanup (after cutover verification)

Once both consumers are confirmed to be working from per-component
config only:

1. **Remove legacy env vars from the chat agent.**  Delete
   `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`,
   `LANGFUSE_COGNEE_PUBLIC_KEY`, and `LANGFUSE_COGNEE_SECRET_KEY` from
   the chat agent's `EnvStore` (via the dashboard UI at
   `PUT /services/robotsix-chat/env` or the chat agent's own settings).

2. **Remove the hand-aggregated project list from the cost monitor.**
   Delete the `projects[]` array from the cost monitor's standardized
   config — the cost monitor now reads the same data from
   `GET /fleet/langfuse`.

3. **Re-verify.**  Run the verification commands from step 4 again.
   Both consumers must still list all expected projects.

## Troubleshooting

**A component's projects don't appear in `/fleet/langfuse`:**

- Ensure the component has `allow_chat_access` or
  `chat_agent_mutatable` enabled (the auto-discovery scanner only
  examines components with one of these toggles on).
- Check that the `langfuse` block is present in the component's config
  and that both `public_key` and `secret_key` are non-empty for each
  project.

**The chat-agent proxy returns "Unknown Langfuse project alias":**

- The proxy only serves projects that have been auto-discovered or
  operator-configured.  Verify the project alias appears in
  `GET /chat/langfuse/projects`.

**After cleanup, a consumer can't access Langfuse:**

- Verify that the component whose project is needed still has
  `allow_chat_access` or `chat_agent_mutatable` enabled.  The
  auto-discovery scanner skips components without these toggles.
