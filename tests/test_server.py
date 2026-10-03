"""Tests for forgejo-mcp's SSE transport wiring and its Host-header policy.

The tool functions need a live Forgejo and are not tested here.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

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
    for var in ("MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MCP_ALLOWED_HOSTS"):
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
