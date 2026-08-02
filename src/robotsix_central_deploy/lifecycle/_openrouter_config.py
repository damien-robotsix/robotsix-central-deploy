"""Canonical reader for a component's OpenRouter provider keys.

A component that generates LLM spend owns the provider key for each of its
LLM functions, under a single top-level ``openrouter`` block::

    "openrouter": {
      "keys": {
        "robotsix-chat": "sk-or-...",
        "robotsix-chat-cognee": "sk-or-..."
      }
    }

The alias keying ``keys`` is the **same alias** used by the ``langfuse.projects``
block (see :mod:`._langfuse_config`) — the Langfuse project name, which the
component standard fixes as ``<repo>`` for a component's main LLM function and
``<repo>-<function>`` for every additional one.

Sharing the alias is the point: reconciliation compares what a provider billed
for one LLM function against what Langfuse traced for that same function, so
the two credentials have to be joinable. A component with two tracing functions
declares two entries here, exactly as it declares two Langfuse projects.

This module is the single place that knows the block's shape.

As with Langfuse credentials there is deliberately **no fallback** to
pre-standard config shapes (chat's ``llmio_api_key``, mill's
``secrets.openrouter_api_key``) or to deploy-plane environment variables: a
component that has not migrated reports no keys, which is the intended, visible
failure. Operators can bridge an unmigrated component through
``LifecycleConfig.openrouter_keys`` without the component changing.
"""

from __future__ import annotations

__all__ = ["extract_openrouter_keys"]


def extract_openrouter_keys(config_dict: dict[str, object]) -> dict[str, str]:
    """Return ``{alias: openrouter_key}`` from a component's standardized config.

    *config_dict* is a component's current config values as stored by
    ``ConfigYamlStore``. Entries with an empty key are skipped — unconfigured,
    rather than a broken credential to hand out.

    Returns an empty dict when the component declares no ``openrouter`` block.
    """
    openrouter = config_dict.get("openrouter")
    if not isinstance(openrouter, dict):
        return {}

    raw_keys = openrouter.get("keys")
    if not isinstance(raw_keys, dict):
        return {}

    out: dict[str, str] = {}
    for alias, key in raw_keys.items():
        if isinstance(key, str) and key:
            out[str(alias)] = key
    return out
