"""Integration tests for the chat-agent GitHub Actions status endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

pytest.importorskip("github")

import robotsix_central_deploy.lifecycle.app as server_mod


class _FakeRun:
    """Stand-in for a PyGithub ``WorkflowRun``."""

    def __init__(
        self,
        run_id: int,
        *,
        name: str = "CI",
        status: str = "completed",
        conclusion: str | None = "success",
    ) -> None:
        self.id = run_id
        self.name = name
        self.status = status
        self.conclusion = conclusion
        self.head_branch = "main"
        self.head_sha = "abc123"
        self.run_number = 5
        self.event = "push"
        self.html_url = f"https://github.com/acme/widget/actions/runs/{run_id}"
        self.created_at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
        self.updated_at = datetime(2026, 7, 7, 12, 5, 0, tzinfo=UTC)


class _FakePaginatedList:
    """Stand-in for a PyGithub ``PaginatedList``.

    Faithfully reproduces the trait that breaks a naive ``[:n]`` slice:
    slicing past the end raises ``IndexError`` (PyGithub does *not* clamp
    the slice to the available count), while plain iteration is safe. A
    plain Python list — as other tests use — would silently clamp and so
    never exercise the bug the endpoint's ``islice`` guard fixes.
    """

    def __init__(self, items: list[_FakeRun]) -> None:
        self._items = list(items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, key):
        if (
            isinstance(key, slice)
            and key.stop is not None
            and key.stop > len(self._items)
        ):
            raise IndexError("list index out of range")
        return self._items[key]


def _fake_client(repo_obj: MagicMock) -> MagicMock:
    client = MagicMock(name="fake-github-client")
    client.get_repo.return_value = repo_obj
    return client


@pytest.fixture
def enable_github_app():
    """Configure github_app_id/private_key/installation_id so the endpoints don't 503."""
    from pydantic import SecretStr

    server_mod.app.state.config.github_app_id = "12345"
    server_mod.app.state.config.github_app_private_key = SecretStr("pem-data")
    server_mod.app.state.config.installation_id = SecretStr("999")
    yield
    server_mod.app.state.config.github_app_id = ""
    server_mod.app.state.config.github_app_private_key = SecretStr("")
    server_mod.app.state.config.installation_id = SecretStr("")


class TestListWorkflowRuns:
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/actions/runs")
        assert resp.status_code != 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs", headers=auth_headers
        )
        assert resp.status_code == 503

    async def test_lists_runs(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = [_FakeRun(1), _FakeRun(2)]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert body[0] == {
            "id": 1,
            "name": "CI",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_sha": "abc123",
            "run_number": 5,
            "event": "push",
            "html_url": "https://github.com/acme/widget/actions/runs/1",
            "created_at": "2026-07-07T12:00:00+00:00",
            "updated_at": "2026-07-07T12:05:00+00:00",
        }
        repo_obj.get_workflow_runs.assert_called_once_with()

    async def test_passes_branch_and_status_filters(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = []
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs"
            "?branch=main&run_status=in_progress",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        repo_obj.get_workflow_runs.assert_called_once_with(
            branch="main", status="in_progress"
        )

    async def test_per_page_capped_at_100(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = [_FakeRun(i) for i in range(5)]
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs?per_page=999",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert (
            len(resp.json()) == 5
        )  # only 5 available; cap doesn't truncate below that

    async def test_fewer_runs_than_per_page_does_not_500(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """Regression: a real ``PaginatedList`` raises ``IndexError`` when
        sliced past its length, so a repo with fewer runs than ``per_page``
        used to 500. The endpoint must return the available runs instead."""
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = _FakePaginatedList([_FakeRun(1)])
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs?per_page=10",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert len(resp.json()) == 1

    async def test_empty_run_list_returns_empty(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """A repo with no workflow runs returns ``[]``, not a 500."""
        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.return_value = _FakePaginatedList([])
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs", headers=auth_headers
        )

        assert resp.status_code == 200
        assert resp.json() == []

    async def test_unknown_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/ghost/actions/runs", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_not_installed_repo_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        """A repo outside the App's installation scope returns 404, not 500."""
        from github import UnknownObjectException

        async def _raise_not_found(config, owner, repo):
            raise UnknownObjectException(404, data={"message": "Not Found"})

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _raise_not_found,
        )

        resp = await client.get(
            "/chat/github/repos/robotsix/nonexistent/actions/runs",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_generic_github_error_returns_502(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import GithubException

        repo_obj = MagicMock()
        repo_obj.get_workflow_runs.side_effect = GithubException(
            500, data={"message": "boom"}
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs", headers=auth_headers
        )
        assert resp.status_code == 502


class TestGetWorkflowRun:
    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/actions/runs/1")
        assert resp.status_code != 401

    async def test_gets_single_run(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        repo_obj = MagicMock()
        repo_obj.get_workflow_run.return_value = _FakeRun(
            42, status="in_progress", conclusion=None
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/42", headers=auth_headers
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == 42
        assert body["status"] == "in_progress"
        assert body["conclusion"] is None
        repo_obj.get_workflow_run.assert_called_once_with(42)

    async def test_run_not_found_returns_404(
        self, client: AsyncClient, auth_headers: dict, monkeypatch, enable_github_app
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_workflow_run.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/9999", headers=auth_headers
        )
        assert resp.status_code == 404


class TestListWorkflowRunJobs:
    """Tests for ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}/jobs``."""

    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/actions/runs/1/jobs")
        assert resp.status_code != 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/1/jobs",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_lists_jobs(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        class _FakeStep:
            def __init__(self, name, status, conclusion, number):
                self.name = name
                self.status = status
                self.conclusion = conclusion
                self.number = number
                self.started_at = datetime(2026, 7, 7, 12, 0, 10, tzinfo=UTC)
                self.completed_at = datetime(2026, 7, 7, 12, 0, 30, tzinfo=UTC)

        class _FakeJob:
            def __init__(self, job_id, name, status, conclusion, steps):
                self.id = job_id
                self.name = name
                self.status = status
                self.conclusion = conclusion
                self.started_at = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
                self.completed_at = datetime(2026, 7, 7, 12, 1, 0, tzinfo=UTC)
                self.html_url = f"https://github.com/acme/widget/runs/1/jobs/{job_id}"
                self.steps = steps

        jobs = [
            _FakeJob(
                101,
                "build",
                "completed",
                "success",
                [
                    _FakeStep("Checkout", "completed", "success", 1),
                    _FakeStep("Build", "completed", "success", 2),
                ],
            ),
            _FakeJob(
                102,
                "test",
                "completed",
                "failure",
                [
                    _FakeStep("Checkout", "completed", "success", 1),
                    _FakeStep("Test", "completed", "failure", 2),
                ],
            ),
        ]

        class _FakeJobsPaginated:
            def __init__(self, items):
                self._items = list(items)

            def __iter__(self):
                return iter(self._items)

            def __getitem__(self, key):
                if (
                    isinstance(key, slice)
                    and key.stop is not None
                    and key.stop > len(self._items)
                ):
                    raise IndexError("list index out of range")
                return self._items[key]

        fake_run = MagicMock()
        fake_run.jobs.return_value = _FakeJobsPaginated(jobs)

        repo_obj = MagicMock()
        repo_obj.get_workflow_run.return_value = fake_run
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/1/jobs",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["id"] == 101
        assert data[0]["name"] == "build"
        assert data[0]["status"] == "completed"
        assert data[0]["conclusion"] == "success"
        assert data[0]["html_url"] == ("https://github.com/acme/widget/runs/1/jobs/101")
        assert len(data[0]["steps"]) == 2
        assert data[0]["steps"][0]["name"] == "Checkout"
        assert data[0]["steps"][0]["status"] == "completed"
        assert data[0]["steps"][0]["conclusion"] == "success"
        assert data[0]["steps"][0]["number"] == 1
        assert data[1]["id"] == 102
        assert data[1]["conclusion"] == "failure"

        repo_obj.get_workflow_run.assert_called_once_with(1)

    async def test_run_not_found_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from github import UnknownObjectException

        repo_obj = MagicMock()
        repo_obj.get_workflow_run.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )
        fake_client = _fake_client(repo_obj)

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/9999/jobs",
            headers=auth_headers,
        )
        assert resp.status_code == 404


class TestGetWorkflowRunLogs:
    """Tests for ``GET /chat/github/repos/{owner}/{repo}/actions/runs/{run_id}/logs``."""

    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get("/chat/github/repos/acme/widget/actions/runs/1/logs")
        assert resp.status_code != 401

    async def test_503_when_app_not_configured(
        self,
        client: AsyncClient,
        auth_headers: dict,
    ):
        # Do NOT use enable_github_app — the app must be unconfigured for 503.
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/1/logs",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_gets_logs(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_and_extract_run_logs",
            lambda token, owner, repo, run_id, job_filter=None, tail_kb=100: (
                "=== Deploy to OVH/1_Set up job.txt ===\n"
                "Run deploy.sh\n"
                "Uploading via lftp...\n"
            ),
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/10/logs",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert "Deploy to OVH" in resp.text
        assert "lftp" in resp.text

    async def test_job_filter_passed_through(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        captured_kwargs: dict = {}

        def _fake_fetch(token, owner, repo, run_id, **kwargs):
            captured_kwargs.update(kwargs)
            return "filtered logs"

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_and_extract_run_logs",
            _fake_fetch,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/10/logs?job=Deploy&tail_kb=50",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert captured_kwargs.get("job_filter") == "Deploy"
        assert captured_kwargs.get("tail_kb") == 50

    async def test_repo_not_found_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from github import UnknownObjectException

        def _raise_not_found(app_id, private_key, installation_id):
            raise UnknownObjectException(404, data={"message": "Not Found"})

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            _raise_not_found,
        )

        resp = await client.get(
            "/chat/github/repos/acme/ghost/actions/runs/1/logs",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_run_not_found_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from fastapi import HTTPException

        def _raise_404(token, owner, repo, run_id, **kwargs):
            raise HTTPException(status_code=404, detail="Run 9999 not found")

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_and_extract_run_logs",
            _raise_404,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/9999/logs",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_fetch_failure_returns_502(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        def _raise_runtime(token, owner, repo, run_id, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_and_extract_run_logs",
            _raise_runtime,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/1/logs",
            headers=auth_headers,
        )
        assert resp.status_code == 502

    async def test_log_singular_returns_same_result(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        """``/log`` (singular) is an alias for ``/logs`` and returns the same output."""
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_and_extract_run_logs",
            lambda token, owner, repo, run_id, job_filter=None, tail_kb=100: (
                "=== Deploy to OVH/1_Set up job.txt ===\n"
                "Run deploy.sh\n"
                "Uploading via lftp...\n"
            ),
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/runs/10/log",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert "Deploy to OVH" in resp.text
        assert "lftp" in resp.text


class TestGetJobLogs:
    """Tests for ``GET /chat/github/repos/{owner}/{repo}/actions/jobs/{job_id}/logs``."""

    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/jobs/42/logs",
        )
        assert resp.status_code != 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/jobs/42/logs",
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_gets_job_log(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_job_log",
            lambda token, owner, repo, job_id, tail_kb=100: (
                "2026-07-31T10:00:00Z | Starting job\n"
                "2026-07-31T10:01:00Z | Running mypy...\n"
                "2026-07-31T10:02:00Z | mypy failed with 3 errors\n"
            ),
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/jobs/42/logs",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert "mypy failed" in resp.text
        assert "Starting job" in resp.text

    async def test_tail_kb_passed_through(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        captured_kwargs: dict = {}

        def _fake_fetch(token, owner, repo, job_id, **kwargs):
            captured_kwargs.update(kwargs)
            return "log text"

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_job_log",
            _fake_fetch,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/jobs/42/logs?tail_kb=50",
            headers=auth_headers,
        )

        assert resp.status_code == 200
        assert captured_kwargs.get("tail_kb") == 50

    async def test_job_not_found_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from fastapi import HTTPException

        def _raise_404(token, owner, repo, job_id, **kwargs):
            raise HTTPException(status_code=404, detail="Job 9999 not found")

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_job_log",
            _raise_404,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/jobs/9999/logs",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_fetch_failure_returns_502(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        def _raise_runtime(token, owner, repo, job_id, **kwargs):
            raise RuntimeError("connection reset")

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "get_installation_token_sync",
            lambda app_id, private_key, installation_id: "fake-token",
        )
        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions."
            "_fetch_job_log",
            _raise_runtime,
        )

        resp = await client.get(
            "/chat/github/repos/acme/widget/actions/jobs/42/logs",
            headers=auth_headers,
        )
        assert resp.status_code == 502

    async def test_redirect_follow_mocked(
        self,
        monkeypatch,
    ):
        """Verify that _fetch_job_log follows the 302 redirect to the signed URL.

        GitHub's job-logs endpoint returns 302 → Location → signed URL.
        The second GET must NOT include the Authorization header.
        """
        from unittest.mock import MagicMock

        from robotsix_central_deploy.lifecycle.routers.chat_github_actions import (
            _fetch_job_log,
        )

        # Build fake responses
        redirect_response = MagicMock()
        redirect_response.status_code = 302
        redirect_response.headers = {
            "Location": "https://objects.githubusercontent.com/signed-url"
        }

        log_response = MagicMock()
        log_response.status_code = 200
        log_response.text = "line 1\nline 2\nline 3\n"

        # The httpx.Client is used as a context manager; mock
        # __enter__ to return itself, and mock get() to return the two
        # responses in order.
        fake_client = MagicMock()
        fake_client.__enter__.return_value = fake_client
        fake_client.get.side_effect = [redirect_response, log_response]

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github_actions.httpx.Client",
            lambda **kwargs: fake_client,
        )

        result = _fetch_job_log(
            "token-abc",
            "acme",
            "widget",
            42,
            tail_kb=0,
        )
        assert result == "line 1\nline 2\nline 3\n"

        # Verify two GET calls were made
        assert fake_client.get.call_count == 2

        # First call: to the GitHub API (with Authorization header)
        call_1_args, call_1_kwargs = fake_client.get.call_args_list[0]
        assert "api.github.com/repos/acme/widget/actions/jobs/42/logs" in call_1_args[0]
        assert (
            call_1_kwargs.get("headers", {}).get("Authorization") == "Bearer token-abc"
        )

        # Second call: to the signed URL (NO Authorization header)
        call_2_args, call_2_kwargs = fake_client.get.call_args_list[1]
        assert call_2_args[0] == "https://objects.githubusercontent.com/signed-url"
        # headers dict should not be passed in the second call
        assert "headers" not in call_2_kwargs or call_2_kwargs.get("headers") is None


class TestDispatchWorkflow:
    @pytest.fixture(autouse=True)
    def _clear_audit(self):
        """Ensure audit store is clean before each test."""
        server_mod.app.state.chat_agent_audit_store._entries = []
        yield

    async def test_unauthorized_no_longer_401(self, client: AsyncClient):
        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/deploy.yml/dispatches",
            json={"ref": "main"},
        )
        assert resp.status_code != 401

    async def test_503_when_app_not_configured(
        self, client: AsyncClient, auth_headers: dict
    ):
        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/deploy.yml/dispatches",
            json={"ref": "main"},
            headers=auth_headers,
        )
        assert resp.status_code == 503

    async def test_dispatches_workflow(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {},
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/deploy-ovh.yml/dispatches",
            json={"ref": "main", "inputs": {"environment": "production"}},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "dispatched": True,
            "workflow": "deploy-ovh.yml",
            "ref": "main",
        }
        fake_client.requester.requestJsonAndCheck.assert_called_once_with(
            "POST",
            "/repos/acme/widget/actions/workflows/deploy-ovh.yml/dispatches",
            input={"ref": "main", "inputs": {"environment": "production"}},
        )

    async def test_dispatches_workflow_without_inputs(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {},
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/deploy.yml/dispatches",
            json={"ref": "main"},
            headers=auth_headers,
        )

        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "dispatched": True,
            "workflow": "deploy.yml",
            "ref": "main",
        }
        fake_client.requester.requestJsonAndCheck.assert_called_once_with(
            "POST",
            "/repos/acme/widget/actions/workflows/deploy.yml/dispatches",
            input={"ref": "main", "inputs": {}},
        )

    async def test_records_audit_entry(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.return_value = (
            {"Content-Type": "application/json"},
            {},
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/deploy-ovh.yml/dispatches",
            json={"ref": "main", "inputs": {"environment": "production"}},
            headers=auth_headers,
        )
        assert resp.status_code == 200

        entries = await server_mod.app.state.chat_agent_audit_store.list()
        assert len(entries) == 1
        assert entries[0].component == "github"
        assert entries[0].action == "dispatch_workflow"
        assert entries[0].key == "acme/widget/deploy-ovh.yml"
        assert entries[0].new_value == {
            "ref": "main",
            "inputs": {"environment": "production"},
        }

    async def test_unknown_workflow_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from github import UnknownObjectException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.side_effect = UnknownObjectException(
            404, data={"message": "Not Found"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/missing.yml/dispatches",
            json={"ref": "main"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_github_rejects_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        from github import GithubException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.side_effect = GithubException(
            422,
            data={"message": "Workflow does not have a 'workflow_dispatch' trigger"},
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/no-dispatch.yml/dispatches",
            json={"ref": "main"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_generic_github_error_returns_502(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        """A GithubException with a non-422 status (e.g. 500) should map to 502."""
        from github import GithubException

        fake_client = MagicMock(name="fake-github-client")
        fake_client.requester = MagicMock()
        fake_client.requester.requestJsonAndCheck.side_effect = GithubException(
            500, data={"message": "Internal Server Error"}
        )

        async def _fake_get_client(config, owner, repo):
            return fake_client

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _fake_get_client,
        )

        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/deploy.yml/dispatches",
            json={"ref": "main"},
            headers=auth_headers,
        )
        assert resp.status_code == 502

    async def test_repo_not_installed_returns_404(
        self,
        client: AsyncClient,
        auth_headers: dict,
        monkeypatch,
        enable_github_app,
    ):
        """A repo outside the App's installation scope returns 404 from
        ``_get_client_or_503`` before the workflow dispatch is attempted."""
        from github import UnknownObjectException

        async def _raise_not_found(config, owner, repo):
            raise UnknownObjectException(404, data={"message": "Not Found"})

        monkeypatch.setattr(
            "robotsix_central_deploy.lifecycle.routers.chat_github.get_github_client",
            _raise_not_found,
        )

        resp = await client.post(
            "/chat/github/repos/acme/nonexistent/actions/workflows/deploy.yml/dispatches",
            json={"ref": "main"},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_missing_ref_returns_422(
        self,
        client: AsyncClient,
        auth_headers: dict,
        enable_github_app,
    ):
        resp = await client.post(
            "/chat/github/repos/acme/widget/actions/workflows/deploy.yml/dispatches",
            json={"inputs": {"env": "prod"}},
            headers=auth_headers,
        )
        assert resp.status_code == 422
