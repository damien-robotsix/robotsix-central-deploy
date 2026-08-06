Get `main`'s CI back to green. Three independent pre-existing failures
were blocking every PR — and, via mill's target-branch-debt guard, 9
central-deploy tickets: a stale `openapi.json`, `cryptography` 49.0.0
carrying GHSA-g6cj-pr64-35w5, and a TruffleHog false positive where the
Lob detector reported the pytest function name
`test_start_container_none_returns_failed` as a verified secret.
