Regenerate `docs/lifecycle/openapi.json` for the `warning` and
`refresh_capable` fields added by the claude-auth refresh-scope fix. The
spec was not regenerated with that change, so the `openapi-drift` CI job
had been failing on `main` — which in turn tripped mill's
target-branch-debt guard and blocked 9 central-deploy tickets from
merging.
