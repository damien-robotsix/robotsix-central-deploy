"""Shared sibling fan-out helpers.

Extracted from services.py and chat.py to eliminate duplicated best-effort
fan-out boilerplate for start / stop / restart lifecycle actions.
"""

from __future__ import annotations

import logging
from typing import Literal

from .._config_utils import _sanitize_log
from ..backends import ExecutionBackend
from ..deps import _get_sibling_pairs
from ..store import ServiceStore
from ...registry.models import ComponentConfig

logger = logging.getLogger(__name__)


async def _fanout_siblings_best_effort(
    name: str,
    config: ComponentConfig,
    store: ServiceStore,
    backend: ExecutionBackend,
    action: Literal["start", "stop", "restart"],
) -> None:
    """Fan out *action* to every sibling of *name* (best-effort per sibling).

    Each sibling is handled independently — a failure in one does not
    prevent the others from being processed.  Missing sibling records
    are logged and skipped by ``_get_sibling_pairs``.
    """
    backend_method = getattr(backend, action)

    for sib, sib_record in await _get_sibling_pairs(name, config, store):
        try:
            final = await backend_method(sib_record)
            sib_record.state = final
            await store.put(sib_record)
        except Exception:
            logger.warning(
                "%s sibling '%s-%s' failed",
                action,
                _sanitize_log(name),
                _sanitize_log(sib.service_key),
            )
