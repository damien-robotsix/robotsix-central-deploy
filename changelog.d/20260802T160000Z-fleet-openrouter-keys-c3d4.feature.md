`GET /fleet/langfuse` now also returns each project's OpenRouter key, joined to
its Langfuse project by the shared alias, so a consumer can reconcile
provider-billed spend against traced spend for the same LLM function without a
second lookup. Keys come from a component's canonical `openrouter.keys.<alias>`
block (new `_openrouter_config.py`, no fallback to pre-standard shapes) or from
the operator-owned `LifecycleConfig.openrouter_keys` map, which overrides on
alias collision and bridges components that have not migrated.
