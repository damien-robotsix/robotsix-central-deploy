# vulture_whitelist.py — dead-code allowlist for `uv run vulture`.
#
# Parse-only file (never imported): vulture counts any name mentioned here
# as used. Each entry cites the flagged location. Grouped by why the finding
# is not actionable; keep new entries in the matching section.

# ===========================================================================
# Serialized API-contract fields — pydantic/dataclass model attributes that
# are populated for JSON responses (or parsed from requests) and never read
# as attributes by application code.
# ===========================================================================
severity  # unused variable (src/robotsix_central_deploy/caretaker/models.py:67)
prev_size_bytes  # unused variable (src/robotsix_central_deploy/caretaker/volume_audit/models.py:36)
last_scan_at  # unused variable (src/robotsix_central_deploy/caretaker/volume_audit/models.py:76)
latest_digest  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:232)
sibling_health  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:234)
overall_health  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:236)
previous_state  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:261)
current_state  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:262)
rolled_back_to_digest  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:335)
current_state  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:336)
images_size_bytes  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:385)
dangling_images_bytes  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:386)
dangling_images_reclaimable_bytes  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:387)
build_cache_size_bytes  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:388)
build_cache_reclaimable_bytes  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:389)
warn_threshold_pct  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:415)
supported  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:440)
latest_digest  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:444)
updater_container_id  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:452)
openrouter_key  # unused variable (src/robotsix_central_deploy/lifecycle/routers/fleet_langfuse.py:66)
langfuse_host  # unused variable (src/robotsix_central_deploy/lifecycle/routers/fleet_langfuse.py:83)
_.sibling_health  # unused attribute (src/robotsix_central_deploy/lifecycle/routers/services.py:203)
_.overall_health  # unused attribute (src/robotsix_central_deploy/lifecycle/routers/services.py:205)
added_env  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:224)
added_secrets  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:227)
undeclared  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:230)
note  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:406)
changed_fields  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:438)
refresh_status  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:464)
last_refresh_error  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:468)
refresh_capable  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:472)
previous_state  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:571)
current_state  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:572)
current_state  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:588)
current_state  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:641)
current_state  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:665)
existed  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:708)
updater_container_id  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:719)
pass_fail  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:929)
response_snippet  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:934)
images_removed  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:994)
images_skipped_protected  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:998)
images_skipped_in_use  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:1002)
images_skipped_intermediate  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:1006)
images_skipped_error  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:1011)
images_error_summary  # unused variable (src/robotsix_central_deploy/lifecycle/schemas.py:1015)
label  # unused variable (src/robotsix_central_deploy/registry/models.py:101)

# ===========================================================================
# Enum members that are part of the API/state-machine vocabulary, matched by
# value rather than referenced by attribute.
# ===========================================================================
UPDATE_APPLIED  # unused variable (src/robotsix_central_deploy/caretaker/models.py:33)
NOOP  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:59)
DIR  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:66)
START  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:72)
STOP  # unused variable (src/robotsix_central_deploy/lifecycle/models.py:73)

# ===========================================================================
# Framework/idiom entries: Starlette calls BaseHTTPMiddleware.dispatch; the
# deps package swaps its module __class__ for attribute interception;
# SIDECAR_SUFFIXES documents the exclusion patterns enforced in shell.
# ===========================================================================
SIDECAR_SUFFIXES  # unused variable (src/robotsix_central_deploy/caretaker/volume_audit/growth.py:11)
_.__class__  # unused attribute (src/robotsix_central_deploy/lifecycle/deps/__init__.py:104)
_.dispatch  # unused method (src/robotsix_central_deploy/lifecycle/gateway_docs_middleware.py:46)
_.dispatch  # unused method (src/robotsix_central_deploy/lifecycle/rate_limiter.py:178)

# ===========================================================================
# Known-dead code deliberately deferred to the removal ticket filed with the
# vulture adoption PR: run_config_assist (all backends), ComponentRegistry.
# from_yaml, GithubToken.token_type, _canonical_hash, SecretKeyManager.
# _key_path — each is production-unreferenced and kept alive only by tests.
# ===========================================================================
_canonical_hash  # unused function (src/robotsix_central_deploy/lifecycle/_config_utils.py:339)
_.run_config_assist  # unused method (src/robotsix_central_deploy/lifecycle/backends/base.py:137)
_.run_config_assist  # unused method (src/robotsix_central_deploy/lifecycle/backends/docker_cli.py:199)
_.run_config_assist  # unused method (src/robotsix_central_deploy/lifecycle/backends/docker_sdk.py:832)
_.run_config_assist  # unused method (src/robotsix_central_deploy/lifecycle/backends/noop.py:99)
_.token_type  # unused property (src/robotsix_central_deploy/lifecycle/github_app.py:70)
_.from_yaml  # unused method (src/robotsix_central_deploy/registry/loader.py:30)
_._key_path  # unused attribute (src/robotsix_central_deploy/registry/secret_key.py:21)
