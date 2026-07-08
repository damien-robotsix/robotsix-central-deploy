"""Shared fixtures for all integration tests.

Provides a session-scoped ``app``, a function-scoped autouse
``_reset_globals`` that wires a fresh store/backend/config into the
server module before each test, and a function-scoped ``client``
(httpx AsyncClient) backed by an ASGI transport.
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

# ---------------------------------------------------------------------------
# structlog may not be installed in lightweight test environments (e.g.
# sandbox CI).  The lifecycle server's _logging.py does a top-level
# ``import structlog`` that cannot be avoided once *any* module under
# ``robotsix_central_deploy.lifecycle`` is imported.  Inject a minimal
# mock before the first real import so conftest and all downstream tests
# can load without a ModuleNotFoundError.
# ---------------------------------------------------------------------------
try:
    __import__("structlog")
    _STRUCTLOG_REAL = True
except ImportError:
    _STRUCTLOG_REAL = False
    _s = MagicMock()
    # Classes that LOGGING_CONFIG instantiates at module level
    _s.processors.JSONRenderer = MagicMock
    _s.processors.TimeStamper = MagicMock

    # Attributes referenced but not called at import time
    # Use a dedicated local class so we don't mutate the global
    # MagicMock class when setting remove_processors_meta below.
    class _MockProcessorFormatter:
        remove_processors_meta = MagicMock()

    _s.stdlib.ProcessorFormatter = _MockProcessorFormatter
    _s.stdlib.add_log_level = MagicMock()
    _s.stdlib.add_logger_name = MagicMock()
    sys.modules["structlog"] = _s
    sys.modules["structlog.stdlib"] = _s.stdlib
    sys.modules["structlog.processors"] = _s.processors
# ---------------------------------------------------------------------------

import pytest
from httpx import ASGITransport, AsyncClient

from robotsix_central_deploy.lifecycle.backends import NoopBackend
from robotsix_central_deploy.lifecycle.config import LifecycleConfig
from robotsix_central_deploy.lifecycle.deps import JobRegistry
from robotsix_central_deploy.lifecycle.models import ExecutionBackendType
from robotsix_central_deploy.lifecycle.rate_limiter import RateLimitStore
from robotsix_central_deploy.lifecycle.session import SessionStore
from robotsix_central_deploy.lifecycle.store import InMemoryStore
from robotsix_central_deploy.registry.chat_agent_audit_store import ChatAgentAuditStore
from robotsix_central_deploy.registry.config_store import ComponentConfigStore
from robotsix_central_deploy.registry.config_yaml_store import ConfigYamlStore
from robotsix_central_deploy.registry.deploy_history_store import DeployHistoryStore
from robotsix_central_deploy.registry.env_store import EnvStore
from robotsix_central_deploy.registry.loader import ComponentRegistry
from robotsix_central_deploy.registry.secret_key import SecretKeyManager
from robotsix_central_deploy.registry.settings_store import SystemSettingsStore

from robotsix_central_deploy.lifecycle import server as server_mod


def pytest_ignore_collect(collection_path, config):
    """Skip ``test_logging_config.py`` when structlog is not installed."""
    if collection_path.name == "test_logging_config.py":
        try:
            import structlog  # noqa: F401
        except ImportError:
            return True
    return None


@pytest.fixture(scope="session")
def app():
    """Session-scoped FastAPI application."""
    return server_mod.app


@pytest.fixture(autouse=True)
def _reset_globals(monkeypatch, tmp_path):
    """Wire a fresh store/backend/config into the server module before each test.

    All file-backed stores use the per-test ``tmp_path`` so tests stay
    isolated under parallel (xdist) execution.  The union of every field
    required across the lifecycle, onboard, UI, and gateway test suites
    is wired here so sub-package conftests and inline helpers can
    eventually delegate to this single source of truth.
    """
    monkeypatch.setenv("ROBOTSIX_LIFECYCLE_API_KEY", "test-key")
    cfg = LifecycleConfig(  # type: ignore[call-arg]
        store_backend="memory",
        execution_backend=ExecutionBackendType.NOOP,
        api_key="test-key",
    )
    store = InMemoryStore()
    backend = NoopBackend()

    # Registry checker mock
    mock_checker = MagicMock()
    mock_checker.get_latest_digest = AsyncMock(return_value=None)

    # File-backed stores isolated under a subdirectory to avoid
    # collisions with tests that inspect tmp_path directly.
    state_dir = tmp_path / "_pytest_conftest"
    state_dir.mkdir(exist_ok=True)
    km = SecretKeyManager(state_dir / "secrets.key")
    env_store = EnvStore(state_dir / "env.json", km)
    config_store = ComponentConfigStore(state_dir / "config_store.json")
    config_yaml_store = ConfigYamlStore(state_dir / "config_yaml.json")
    deploy_history_store = DeployHistoryStore(state_dir / "deploy_history.json")
    chat_agent_audit_store = ChatAgentAuditStore(state_dir / "chat_agent_audit.json")
    settings_path = state_dir / "settings.json"
    settings_store = SystemSettingsStore(settings_path)
    registry = ComponentRegistry([])

    session_store = SessionStore()
    rate_limit_store = RateLimitStore()
    job_registry = JobRegistry()

    # Set both the module-level globals and app.state so all code paths work.
    server_mod._config = cfg
    server_mod._store = store
    server_mod._backend = backend
    server_mod._registry_checker = mock_checker
    server_mod.app.state.config = cfg
    server_mod.app.state.store = store
    server_mod.app.state.backend = backend
    server_mod.app.state.registry_checker = mock_checker
    server_mod.app.state.key_manager = km
    server_mod.app.state.env_store = env_store
    server_mod.app.state.config_yaml_store = config_yaml_store
    server_mod.app.state.deploy_history_store = deploy_history_store
    server_mod.app.state.chat_agent_audit_store = chat_agent_audit_store
    server_mod.app.state.chat_agent_rate_limits = {}
    server_mod.app.state.component_config_store = config_store
    server_mod.app.state.registry = registry
    server_mod.app.state.job_registry = job_registry
    server_mod.app.state.session_store = session_store
    server_mod.app.state.settings_store = settings_store
    server_mod.app.state.rate_limit_store = rate_limit_store
    server_mod.app.state.http_client = MagicMock(spec=AsyncClient)


@pytest.fixture(autouse=True)
def _mock_structlog(monkeypatch):
    """Ensure ``structlog`` is available as a mock in ``sys.modules``.

    The ``lifecycle.cli`` module imports ``_logging``, which does a
    top-level ``import structlog``.  In environments where structlog
    is not installed (including the test sandbox), this fixture
    prevents a ``ModuleNotFoundError`` during CLI argument-parsing
    tests.  When structlog *is* installed (e.g. CI) the real module
    is left untouched.
    """
    try:
        import structlog  # noqa: F401
    except ImportError:
        mock = MagicMock()
        # Provide the minimal surface that _logging.py references.
        mock.stdlib.ProcessorFormatter = MagicMock()
        monkeypatch.setitem(sys.modules, "structlog", mock)


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"X-API-Key": "test-key"}


@pytest.fixture
async def client(app):
    """Function-scoped httpx AsyncClient wired to the FastAPI app via ASGI transport."""
    transport = ASGITransport(app=app)  # type: ignore[arg-type]
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def pytest_ignore_collect(collection_path, config):
    if (
        hasattr(collection_path, "name")
        and collection_path.name == "test_logging_config.py"
    ):
        if not _STRUCTLOG_REAL:
            return True
    return False
