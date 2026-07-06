# Registry Checker

The `RegistryChecker` (`src/robotsix_central_deploy/registry_check/checker.py`)
polls container registries for the latest manifest digest of managed images.

## Supported Registries

| Registry | Host | Auth | Manifest Host |
|----------|------|------|---------------|
| **GHCR** | `ghcr.io` (explicit) | `_fetch_ghcr_token` | `ghcr.io` |
| **Docker Hub** | `docker.io` (explicit) or implicit (no `.`/`:` in first segment) | `_fetch_dockerhub_token` | `registry-1.docker.io` |

## Repo Derivation

**Docker Hub:**
- `docker.io/robotsix/mill:latest` → `robotsix/mill`
- `robotsix/mill:latest` (implicit) → `robotsix/mill`
- `nginx:latest` (single-segment implicit) → `library/nginx`

## Cache

Entries are cached with a configurable TTL (default 300 s). A stale entry
triggers a fresh fetch on next lookup.
