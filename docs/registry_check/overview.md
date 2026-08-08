# Registry Checker

The `RegistryChecker` (`src/robotsix_central_deploy/registry_check/checker.py`)
polls container registries for the latest manifest digest of managed images.

## Supported Registries

| Registry | Host | Auth | Manifest Host |
|----------|------|------|---------------|
| **GHCR** | `ghcr.io` (explicit) | `_fetch_ghcr_token` (fleet credential) | `ghcr.io` |
| **Docker Hub** | `docker.io` (explicit) or implicit (no `.`/`:` in first segment) | `_fetch_dockerhub_token` | `registry-1.docker.io` |

## GHCR Authentication

The GHCR token exchange presents the **same fleet-wide credential as the image
pull**, resolved by `GhcrCredentialResolver`
(`src/robotsix_central_deploy/_ghcr_auth.py`): the static `ghcr_pull_token`
PAT first, then a GitHub App installation token, then anonymous. Both paths
share one resolver instance, so a token updated at runtime takes effect on
both at once.

Anonymously, a **private** package 401s at the token exchange and its update
status can only ever be reported as unknown — while pulls of the same image
succeed. Auth failures (401/403, at either the token exchange or the manifest
`HEAD`) log a `registry auth failed` warning naming the fix, instead of
silently reporting "unknown".

## Repo Derivation

**Docker Hub:**

- `docker.io/robotsix/mill:latest` → `robotsix/mill`
- `robotsix/mill:latest` (implicit) → `robotsix/mill`
- `nginx:latest` (single-segment implicit) → `library/nginx`

## Cache

Entries are cached with a configurable TTL (default 300 s). A stale entry
triggers a fresh fetch on next lookup.
