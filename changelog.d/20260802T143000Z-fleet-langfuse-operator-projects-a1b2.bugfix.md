`GET /fleet/langfuse` now honours operator-configured Langfuse credentials
(`LifecycleConfig.langfuse_projects`), applying the same precedence the chat
trace proxy already used: operator entries override a component-declared
alias, and operator aliases no component declares are surfaced under a
synthetic `operator-configured` entry. Previously the endpoint read only the
canonical per-component `langfuse.projects` block — which no component has
migrated to yet — so it returned zero projects fleet-wide and left
cost-monitor's dashboard blind.
