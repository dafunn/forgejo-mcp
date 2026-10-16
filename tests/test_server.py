"""Tests for forgejo-mcp's transport wiring, Host-header policy, repo allowlist,
and how refusals reach a client. The forge itself is stubbed.
"""
from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import server as srv  # noqa: E402


@pytest.fixture
def reload_with(monkeypatch):
    """Reimport the module with a given environment; transport config is read at import."""
    def _set(**env):
        for k, v in env.items():
            monkeypatch.setenv(k, v)
        return importlib.reload(srv)

    yield _set
    for var in (
        "MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MCP_ALLOWED_HOSTS",
        "REPO_ALLOWLIST", "FORGEJO_DEFAULT_REPO",
    ):
        monkeypatch.delenv(var, raising=False)
    importlib.reload(srv)


def test_stdio_is_the_default(reload_with):
    mod = reload_with(MCP_ALLOWED_HOSTS="")
    assert mod.MCP_TRANSPORT == "stdio"


def test_sse_requires_allowed_hosts(reload_with):
    """An empty list is mcp's default and rejects every request; refuse to start instead."""
    mod = reload_with(MCP_TRANSPORT="sse", MCP_ALLOWED_HOSTS="")
    with pytest.raises(RuntimeError, match="MCP_ALLOWED_HOSTS is required"):
        mod._transport_security()


def test_allowed_hosts_are_passed_through(reload_with):
    mod = reload_with(MCP_TRANSPORT="sse", MCP_ALLOWED_HOSTS="forgejo-mcp:3000,localhost:3000")
    settings = mod._transport_security()
    assert settings.enable_dns_rebinding_protection is True
    assert settings.allowed_hosts == ["forgejo-mcp:3000", "localhost:3000"]


def test_wildcard_disables_the_host_check(reload_with):
    mod = reload_with(MCP_TRANSPORT="sse", MCP_ALLOWED_HOSTS="*")
    assert mod._transport_security().enable_dns_rebinding_protection is False


def test_build_sse_app_constructs(reload_with):
    """Catches a transport API mismatch that the tool functions never touch."""
    mod = reload_with(MCP_TRANSPORT="sse", MCP_HOST="0.0.0.0", MCP_ALLOWED_HOSTS="forgejo-mcp:3000")
    app = mod.build_sse_app()
    paths = [getattr(r, "path", None) for r in app.routes]
    assert "/sse" in paths


class _Resp:
    def __init__(self, payload=None, text=""):
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload


def _stub_get(monkeypatch, mod, runs):
    """Replace `_get` with a fake forge; `runs` maps run id -> list of job ids."""
    calls = []

    def fake(path, params=None):
        calls.append(path)
        if path.endswith("/actions/runs"):
            return _Resp({"workflow_runs": [{"id": rid, "index_in_repo": rid} for rid in runs]})
        if path.endswith("/jobs"):
            rid = int(path.split("/runs/")[1].split("/")[0])
            return _Resp({"workflow_jobs": [{"id": j} for j in runs[rid]]})
        if path.endswith("/logs"):
            return _Resp(text="log line")
        raise AssertionError(path)

    monkeypatch.setattr(mod, "_get", fake)
    return calls


def test_bad_allowlist_entry_refuses_startup(reload_with):
    with pytest.raises(RuntimeError, match="REPO_ALLOWLIST.*'justname'"):
        reload_with(REPO_ALLOWLIST="owner/ok,justname")


@pytest.mark.parametrize("entry", ["owner/", "/name", "a/b/c", "../x", "a/.."])
def test_malformed_allowlist_entries_refuse_startup(reload_with, entry):
    with pytest.raises(RuntimeError, match="REPO_ALLOWLIST"):
        reload_with(REPO_ALLOWLIST=entry)


def test_default_repo_off_the_list_refuses_startup(reload_with):
    with pytest.raises(RuntimeError, match="REPO_ALLOWLIST"):
        reload_with(REPO_ALLOWLIST="owner/allowed", FORGEJO_DEFAULT_REPO="owner/other")


def test_padded_default_repo_matching_an_entry_passes(reload_with):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed", FORGEJO_DEFAULT_REPO=" owner/allowed/ ")
    assert mod._repo("") == "owner/allowed"


def test_allowed_repo_resolves_and_other_is_refused(reload_with):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    assert mod._repo("owner/allowed") == "owner/allowed"
    with pytest.raises(ToolError, match="owner/other.*REPO_ALLOWLIST"):
        mod._repo("owner/other")


def test_refusal_does_not_list_allowed_repos(reload_with):
    mod = reload_with(REPO_ALLOWLIST="owner/secret-one,owner/secret-two")
    with pytest.raises(ToolError) as exc:
        mod._repo("owner/other")
    assert "secret" not in str(exc.value)


def test_omitted_repo_uses_default_and_passes(reload_with):
    mod = reload_with(REPO_ALLOWLIST="owner/a,owner/b", FORGEJO_DEFAULT_REPO="owner/b")
    assert mod._repo("") == "owner/b"


def test_match_is_case_insensitive(reload_with):
    mod = reload_with(REPO_ALLOWLIST="Owner/Repo")
    assert mod._repo("owner/repo") == "owner/repo"
    assert mod._repo("OWNER/REPO") == "OWNER/REPO"


def test_trailing_comma_and_spaces_parse_cleanly(reload_with):
    mod = reload_with(REPO_ALLOWLIST=" owner/a , owner/b ,")
    assert mod._repo("owner/a") == "owner/a"
    assert mod._repo("owner/b") == "owner/b"


def test_whitespace_only_allowlist_is_unrestricted(reload_with):
    mod = reload_with(REPO_ALLOWLIST="  ")
    assert mod._repo("any/repo") == "any/repo"


@pytest.mark.parametrize("raw", [",", " , ,"])
def test_allowlist_with_no_entries_refuses_startup(reload_with, raw):
    with pytest.raises(RuntimeError, match="REPO_ALLOWLIST"):
        reload_with(REPO_ALLOWLIST=raw)


def test_unset_allowlist_behaves_as_before(reload_with):
    mod = reload_with(FORGEJO_DEFAULT_REPO="owner/default")
    assert mod._repo("owner/anything") == "owner/anything"
    assert mod._repo("") == "owner/default"
    with pytest.raises(ToolError, match="FORGEJO_DEFAULT_REPO"):
        mod._repo("noslash")


def test_dotdot_repo_is_rejected_in_allowlist_mode(reload_with):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    with pytest.raises(ToolError, match="REPO_ALLOWLIST"):
        mod._repo("owner/..")


def test_refused_repo_makes_no_http_request(reload_with, monkeypatch):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    calls = _stub_get(monkeypatch, mod, {1: [10]})
    with pytest.raises(ToolError):
        mod.forgejo_build_status(repo="owner/other")
    with pytest.raises(ToolError):
        mod.forgejo_job_log(repo="owner/other", job_id=10)
    assert calls == []


def test_job_log_reads_a_job_found_in_the_repos_runs(reload_with, monkeypatch):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    calls = _stub_get(monkeypatch, mod, {1: [10], 2: [20, 21]})
    assert mod.forgejo_job_log(repo="owner/allowed", job_id=21) == "log line"
    assert calls[-1] == "/api/v1/repos/owner/allowed/actions/jobs/21/logs"


def test_job_log_stops_searching_at_the_first_match(reload_with, monkeypatch):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    calls = _stub_get(monkeypatch, mod, {1: [10], 2: [20]})
    mod.forgejo_job_log(repo="owner/allowed", job_id=10)
    assert not any("/runs/2/jobs" in c for c in calls)


def test_job_log_refuses_a_job_not_in_the_repos_runs(reload_with, monkeypatch):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    calls = _stub_get(monkeypatch, mod, {1: [10]})
    with pytest.raises(ToolError, match="not found in the recent runs"):
        mod.forgejo_job_log(repo="owner/allowed", job_id=999)
    assert not any(c.endswith("/logs") for c in calls)


def test_job_log_without_allowlist_skips_the_job_search(reload_with, monkeypatch):
    mod = reload_with()
    calls = _stub_get(monkeypatch, mod, {1: [10]})
    assert mod.forgejo_job_log(repo="owner/any", job_id=999) == "log line"
    assert calls == ["/api/v1/repos/owner/any/actions/jobs/999/logs"]


# -- what a client receives ---------------------------------------------------


def _call(mod, name: str, args: dict):
    async def call():
        async with Client(mod.app) as client:
            return await client.call_tool(name, args)
    return asyncio.run(call())


@pytest.mark.parametrize("name, args, reason", [
    ("forgejo_build_status", {"repo": "owner/other"}, "'owner/other' is not in REPO_ALLOWLIST"),
    ("forgejo_build_status", {"repo": "noslash"}, "repo must be 'owner/name'"),
    ("forgejo_run_info", {"repo": "owner/..", "run_number": 1}, "is not in REPO_ALLOWLIST"),
    ("forgejo_job_log", {"repo": "owner/allowed", "job_id": 999},
     "job 999 was not found in the recent runs of owner/allowed"),
    ("forgejo_job_log", {"repo": "owner/allowed"}, "job_id is required"),
    ("forgejo_run_info", {"repo": "owner/allowed", "run_number": 7}, "No run #7 in the last"),
    ("forgejo_run_jobs", {"repo": "owner/allowed", "run_number": 7}, "No run #7 in the last"),
    ("forgejo_run_log", {"repo": "owner/allowed", "run_number": 7}, "No run #7 in the last"),
])
def test_a_refusal_reaches_the_client_with_its_reason(
        reload_with, monkeypatch, caplog, name, args, reason):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    _stub_get(monkeypatch, mod, {1: [10]})
    with caplog.at_level("DEBUG"):
        result = _call(mod, name, args)
    assert result.is_error is True
    assert reason in result.content[0].text
    assert not [r for r in caplog.records if r.levelname == "ERROR" or r.exc_info]


def test_a_normal_result_is_not_an_error(reload_with, monkeypatch):
    mod = reload_with(REPO_ALLOWLIST="owner/allowed")
    _stub_get(monkeypatch, mod, {1: [10]})
    result = _call(mod, "forgejo_run_jobs", {"repo": "owner/allowed", "run_number": 1})
    assert not result.is_error
    assert "job_id=10" in result.content[0].text


# -- Release version ------------------------------------------------


def test_release_version_is_dev_when_unset(monkeypatch):
    monkeypatch.delenv("FORGEJO_MCP_VERSION", raising=False)
    assert srv.release_version() == "dev"


def test_release_version_is_dev_when_blank(monkeypatch):
    monkeypatch.setenv("FORGEJO_MCP_VERSION", "  ")
    assert srv.release_version() == "dev"


def test_release_version_reports_the_build_value(monkeypatch):
    monkeypatch.setenv("FORGEJO_MCP_VERSION", "0.2.0-3-gabc1234")
    assert srv.release_version() == "0.2.0-3-gabc1234"


def test_server_reports_the_release_version():
    assert srv.app.version == srv.release_version()
