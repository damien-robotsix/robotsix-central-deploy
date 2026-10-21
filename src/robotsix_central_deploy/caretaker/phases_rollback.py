"""Caretaker auto-rollback phase.

Split out of :mod:`.phases`, which re-exports :func:`phase_auto_rollback`.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ..lifecycle.deploy_lock import release_deploy_lock, try_acquire_deploy_lock
from ..lifecycle.models import (
    DeployHistoryEntry,
    DeploySource,
)
from .models import CaretakerFinding, FindingKind

if TYPE_CHECKING:
    from ..lifecycle.backends import ExecutionBackend
    from ..lifecycle.store import ServiceStore
    from ..registry.config_store import ComponentConfigStore
    from ..registry.deploy_history_store import DeployHistoryStore
    from ..registry.env_store import EnvStore

logger = logging.getLogger(__name__)


async def phase_auto_rollback(
    crash_loop_findings: list[CaretakerFinding],
    store: ServiceStore,
    backend: ExecutionBackend,
    component_config_store: ComponentConfigStore,
    deploy_history_store: DeployHistoryStore,
    env_store: EnvStore,
    self_container_name: str = "",
) -> list[CaretakerFinding]:
    """Roll a verified failed deploy back to its previous image (opt-in).

    A ``CRASH_LOOP`` finding from ``phase_restart_watchdog`` is the
    verified-failed-deploy signal (RestartCount grew across a scrape
    interval). When the operator has enabled
    ``caretaker_auto_rollback_enabled`` — the scheduler only calls this
    phase then — each such component is recreated from its
    ``previous_image_digest`` via ``backend.rollback``, restoring the last
    image that was running before the bad deploy (the 2026-09-05 hexarchy
    crash loop would have self-healed this way).

    This is **destructive** (it recreates a running container with a prior
    image), so the behaviour ships behind an OFF-by-default flag and every
    rollback is recorded twice over: a ``ROLLBACK_APPLIED`` finding naming
    the from→to digests, and a ``DeploySource.ROLLBACK`` deploy-history
    entry. A backend failure yields a ``ROLLBACK_FAILED`` finding and never
    aborts the pass.

    Only primary components with a recorded ``previous_image_digest`` are
    rolled back:

    * The self-container (the management plane) is skipped — it cannot
      safely replace itself in-process (mirrors ``phase_update`` and the
      ``POST /services/{name}/rollback`` guard); self-recovery is the
      detached updater's job.
    * A sibling record (or any name without a component config) is skipped
      — ``component_config_store.get`` returns ``None`` for it.
    * A component with no prior digest has nothing to roll back to.
    """
    findings: list[CaretakerFinding] = []

    for finding in crash_loop_findings:
        if finding.kind is not FindingKind.CRASH_LOOP:
            continue
        name = finding.component_id
        if not name:
            continue

        record = await store.get(name)
        if record is None:
            continue

        # Never roll back the container this caretaker runs inside: an
        # in-process recreate would replace the management plane mid-pass
        # (same hazard phase_update guards against). Self-recovery uses the
        # detached updater, not this path.
        if self_container_name and record.container_name == self_container_name:
            logger.debug("phase_auto_rollback: skipping self-component %s", record.name)
            continue

        config = component_config_store.get(record.name)
        if config is None:
            # Siblings and unregistered names have no primary config here.
            logger.debug("phase_auto_rollback: no config for %s, skipping", record.name)
            continue

        if not record.previous_image_digest:
            logger.info(
                "phase_auto_rollback: %s has no prior image digest — nothing "
                "to roll back to",
                record.name,
            )
            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.ROLLBACK_FAILED,
                    title=f"Auto-rollback skipped for {record.name}",
                    detail=(
                        "The component is crash-looping but has no previous "
                        "image digest recorded, so there is no prior image to "
                        "roll back to. Manual intervention is required."
                    ),
                    severity="error",
                )
            )
            continue

        # Serialise against operator/caretaker deploys of the same component.
        if not await try_acquire_deploy_lock(record.name, source="caretaker"):
            logger.info(
                "phase_auto_rollback: deploy already in progress for %s, skipping",
                record.name,
            )
            continue
        try:
            # Recreate the prior container with its full merged env, exactly
            # like phase_update — the static registry env alone would drop
            # EnvStore-provisioned secrets.
            merged_env = await env_store.get_merged_env(record.name, config.env)
            if config.consumed_scopes:
                cred_env = await env_store.resolve_consumed_credentials(
                    record.name, config.consumed_scopes
                )
                if cred_env:
                    merged_env = {**cred_env, **merged_env}
            rollback_config = config.model_copy(update={"env": merged_env})

            old_deployed = record.deployed_image_digest
            old_previous = record.previous_image_digest
            outcome = await backend.rollback(record, rollback_config)

            # Swap digests: rolled-back-to becomes deployed; what we had
            # becomes previous (mirrors the router's one-step rollback).
            record.state = outcome.state
            record.deployed_image_digest = old_previous
            record.previous_image_digest = old_deployed
            record.image_revision = old_previous
            record.last_error = ""
            await store.put(record)

            try:
                await deploy_history_store.append(
                    record.name,
                    DeployHistoryEntry(
                        digest=outcome.deployed_digest,
                        image_ref=record.image,
                        timestamp=time.time(),
                        source=DeploySource.ROLLBACK,
                        previous_digest=old_deployed,
                    ),
                )
            except Exception:
                logger.warning(
                    "phase_auto_rollback: failed to record history for %s",
                    record.name,
                    exc_info=True,
                )

            logger.warning(
                "phase_auto_rollback: rolled %s back %s → %s (crash loop)",
                record.name,
                old_deployed or "unknown",
                outcome.deployed_digest,
            )
            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.ROLLBACK_APPLIED,
                    title=f"Auto-rolled back {record.name} after crash loop",
                    detail=(
                        f"The deploy of {record.name} was verified failed "
                        f"(crash-looping), so the caretaker rolled it back to "
                        f"its previous image.\n"
                        f"From digest: {old_deployed or 'unknown'}\n"
                        f"To digest:   {outcome.deployed_digest}"
                    ),
                    severity="warning",
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "phase_auto_rollback: rollback failed for %s: %s", record.name, exc
            )
            findings.append(
                CaretakerFinding(
                    component_id=record.name,
                    repo_id=config.repo_id,
                    kind=FindingKind.ROLLBACK_FAILED,
                    title=f"Auto-rollback failed for {record.name}",
                    detail=str(exc),
                    severity="error",
                )
            )
        finally:
            release_deploy_lock(record.name)

    return findings
