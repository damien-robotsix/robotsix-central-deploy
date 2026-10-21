"""Background deploy job runner for ``POST /services/{name}/deploy``.

Split out of :mod:`.routers.services_deploy`, which schedules
:func:`_run_deploy_job` and serves the job-status route.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..registry import ComponentConfig, ServiceConfig
from ..registry.config_yaml_store import ConfigYamlStore
from ..registry.deploy_history_store import DeployHistoryStore
from ..registry.env_store import EnvStore
from ..registry.loader import ComponentRegistry
from ._config_utils import (
    _sanitize_log,
    _write_llmio_tier_config,
    config_document_from_template,
)
from .backends import ExecutionBackend, collect_protected_image_refs
from .deploy_lock import release_deploy_lock
from .deploy_verify import rollback_after_failed_deploy, verify_post_deploy_health
from .deps import JobRegistry
from .models import (
    DeployHealthError,
    DeployHistoryEntry,
    DeployJobPhase,
    DeploySource,
    ServiceRecord,
    ServiceState,
)
from .store import ServiceStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Background deploy job runner
# ---------------------------------------------------------------------------


async def _run_deploy_job(
    job_id: str,
    name: str,
    image_ref: str,
    record: ServiceRecord,
    config: ComponentConfig,
    store: ServiceStore,
    backend: ExecutionBackend,
    registry: ComponentRegistry,
    env_store: EnvStore,
    deploy_history_store: DeployHistoryStore,
    config_yaml_store: ConfigYamlStore,
    job_registry: JobRegistry,
    settings_store: Any = None,
    gateway_base_domain: str = "",
) -> None:
    """Background task that runs the full deploy sequence and updates the job.

    On failure the component record is marked FAILED and the job is marked
    failed so the polling endpoint surfaces the error.
    """
    # Imported lazily: ``routers.services_deploy`` imports this module, so a
    # module-level import would be circular.
    from .routers.services_deploy import _fanout_sibling_action, _verify_edge_route

    try:
        # Write merged config.json into the config volume before starting.
        if config.config_volume:
            # The component's own config file is the source of truth, so read
            # it first — both the drift guard and the seed decision below
            # depend on knowing whether it exists.
            try:
                live_dict = await backend.read_config_from_volume(config.config_volume)
            except Exception:  # noqa: BLE001
                live_dict = {}

            # Seed only. The component owns its config; the deploy plane must
            # never write over a config file that already exists.
            #
            # This used to push the stored `current` onto the volume on every
            # deploy, which made the deploy-side copy authoritative in
            # practice: a stale entry silently reverted the component's own
            # settings, and fixing the volume by hand lasted until the next
            # deploy. On 2026-08-07 that overwrote chat's Langfuse credentials
            # with an empty pre-migration block and removed it from fleet-wide
            # Langfuse discovery, with nothing erroring anywhere.
            #
            # A component with no config file yet still needs one, so the
            # shipped template is written when — and only when — the volume is
            # empty.
            if not live_dict:
                template_cfg = await config_yaml_store.get_template(name)
                if template_cfg:
                    # The stored template is a JSON Schema, not a config
                    # document — see config_document_from_template for what
                    # writing it verbatim costs.
                    seed_cfg = config_document_from_template(template_cfg)
                    try:
                        await backend.write_config_to_volume(
                            config.config_volume, seed_cfg
                        )
                        logger.info(
                            "deploy %s: seeded config.json into empty volume %s",
                            _sanitize_log(name),
                            _sanitize_log(config.config_volume),
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "deploy %s: could not seed config.json into volume %s: %s",
                            _sanitize_log(name),
                            _sanitize_log(config.config_volume),
                            exc,
                        )
                        # non-fatal: the component may ship its own default

        # Write the fleet-global llmio tier config (both provider slots,
        # all three levels, plus the failover policy) into the component's
        # config volume so robotsix-llmio's load_tier_config() can resolve
        # any capability level on either slot.
        await _write_llmio_tier_config(
            backend, config, settings_store, name, log_context="deploy"
        )

        # Deploy — update job phase for health-wait visibility.
        if config.health_check is not None and not config.health_check.disable:
            job_registry.update_phase(job_id, DeployJobPhase.WAITING_HEALTH)

        outcome = await backend.deploy(record, config, image_ref)

        record.state = outcome.state
        record.image = image_ref
        record.image_revision = outcome.deployed_digest
        record.deployed_image_digest = outcome.deployed_digest
        record.previous_image_digest = outcome.previous_digest
        record.last_error = ""
        await store.put(record)

        # Record deploy history (best-effort — never fail a successful deploy)
        try:
            await deploy_history_store.append(
                name,
                DeployHistoryEntry(
                    digest=outcome.deployed_digest,
                    image_ref=image_ref,
                    timestamp=time.time(),
                    source=DeploySource.MANUAL,
                    previous_digest=outcome.previous_digest,
                ),
            )
        except Exception:
            logger.warning(
                "deploy %s: failed to record history entry",
                _sanitize_log(name),
                exc_info=True,
            )

        # Deploy siblings
        job_registry.update_phase(job_id, DeployJobPhase.DEPLOYING_SIBLINGS)

        async def _do_deploy_sibling(
            sib_config: ServiceConfig,
            sib_record: ServiceRecord,
            sib_name: str,
            effective_sib: ComponentConfig,
        ) -> None:
            sib_outcome = await backend.deploy(
                sib_record, effective_sib, sib_config.image
            )
            sib_record.state = sib_outcome.state
            sib_record.image = sib_config.image
            sib_record.deployed_image_digest = sib_outcome.deployed_digest
            sib_record.previous_image_digest = sib_outcome.previous_digest
            await store.put(sib_record)
            # Record sibling history (best-effort)
            try:
                await deploy_history_store.append(
                    sib_name,
                    DeployHistoryEntry(
                        digest=sib_outcome.deployed_digest,
                        image_ref=sib_config.image,
                        timestamp=time.time(),
                        source=DeploySource.MANUAL,
                        previous_digest=sib_outcome.previous_digest,
                    ),
                )
            except Exception:
                logger.warning(
                    "deploy sibling '%s': failed to record history",
                    _sanitize_log(sib_name),
                    exc_info=True,
                )

        await _fanout_sibling_action(
            name,
            store,
            registry,
            env_store,
            action=_do_deploy_sibling,
            action_label="deploy",
            consumed_scopes=config.consumed_scopes,
        )

        # Auto-prune dangling images left behind by the update (opt-in setting);
        # rollback targets recorded in the store are protected. Entirely
        # best-effort: a settings-store or prune failure must never fail the
        # deploy that already succeeded.
        try:
            settings = (
                await settings_store.get() if settings_store is not None else None
            )
            if settings is not None and settings.image_auto_prune:
                protected = await collect_protected_image_refs(store)
                result = await backend.prune_images(protected)
                reclaimed = result.space_reclaimed_bytes
                if reclaimed:
                    logger.info(
                        "deploy %s: image auto-prune reclaimed %d bytes",
                        _sanitize_log(name),
                        reclaimed,
                    )
        except Exception:
            logger.warning(
                "deploy %s: image auto-prune failed",
                _sanitize_log(name),
                exc_info=True,
            )

        # Post-deploy health verification: a container can boot, pass its
        # startup healthcheck, and then crash-loop under Docker's restart
        # policy (the 2026-09-05 hexarchy incident: 110 restarts, deploy
        # reported success). Poll runtime state + RestartCount for a window;
        # a restarting container or a growing RestartCount is a FAILED deploy.
        # Applies to remote host: components too — get_container_diagnostics
        # delegates to the right host, and this reads no cosmetic routing field.
        job_registry.update_phase(job_id, DeployJobPhase.VERIFYING)
        verification = await verify_post_deploy_health(backend, record)
        if not verification.ok:
            reason = (
                f"Post-deploy verification failed for '{name}': {verification.reason}"
            )
            logger.error("deploy %s: %s", _sanitize_log(name), reason)
            record.state = ServiceState.FAILED
            record.last_error = reason
            await store.put(record)

            # Roll the crash-looping container back to its last-good image
            # instead of leaving it restarting with no running image recorded
            # (hexarchy-2, 2026-10-12: a failed manual deploy left the broken
            # container restarting with deployed_image_digest=None).
            recovery = await rollback_after_failed_deploy(
                backend,
                record,
                config,
                store,
                deploy_history_store,
                source=DeploySource.ROLLBACK,
                log_context="deploy",
            )
            if recovery.rolled_back:
                reason += (
                    f" — rolled back to the previous image {recovery.rolled_back_to}."
                )
            elif recovery.error:
                reason += (
                    f" — automatic rollback failed ({recovery.error}); the "
                    f"container is still in a failed state."
                )

            job_registry.mark_failed(
                job_id, reason, logs=verification.crash_log or None
            )
            return

        # Verify the edge before calling the deploy done. This is the last
        # step because it needs the replacement container to be up and its
        # labels observed; running it earlier would only measure the recreate
        # window.
        deploy_warnings = list(outcome.warnings)
        try:
            edge_warning = await _verify_edge_route(config, gateway_base_domain)
        except Exception:
            # Never fail a deploy on the check itself.
            logger.warning(
                "deploy %s: edge route check could not run",
                _sanitize_log(name),
                exc_info=True,
            )
            edge_warning = None
        if edge_warning:
            logger.warning("deploy %s: %s", _sanitize_log(name), edge_warning)
            deploy_warnings.append(edge_warning)

        job_registry.mark_done(
            job_id,
            name=name,
            image=image_ref,
            state=record.state.value,
            warnings=deploy_warnings,
        )
    except Exception as exc:
        logger.exception("deploy %s failed", _sanitize_log(name))
        record.state = ServiceState.FAILED
        record.last_error = str(exc)
        # A deploy that recreated the container but then failed its health
        # wait leaves the new (bad) image running; persist that so a later
        # rollback targets the image it actually replaced, not a stale pair
        # two deploys back (2026-10-12 linkedin incident).
        if isinstance(exc, DeployHealthError):
            record.image = image_ref
            record.image_revision = exc.deployed_digest
            record.deployed_image_digest = exc.deployed_digest
            record.previous_image_digest = exc.previous_digest
        await store.put(record)
        # Capture container logs so the operator can diagnose the failure.
        captured_logs: str | None = None
        try:
            captured_logs = await backend.get_container_logs(record, tail=200)
        except Exception:
            logger.warning(
                "deploy %s: failed to capture container logs",
                _sanitize_log(name),
                exc_info=True,
            )
        job_registry.mark_failed(job_id, str(exc), logs=captured_logs)
    finally:
        release_deploy_lock(name)
