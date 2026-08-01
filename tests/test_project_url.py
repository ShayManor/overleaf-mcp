"""Tests for the canonical project-URL helper and its use in write-tool tags.

The MCP client (chatui) turns the opaque project-id jumble on a tool-call
line into a clickable link by harvesting a full ``…/project/<id>`` URL out
of the tool result. For that to work on EVERY project-scoped tool — not
just ``create_project`` — the server must embed the canonical URL, derived
from ``OVERLEAF_BASE_URL`` so self-hosted deployments link correctly.
"""

import importlib
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

_PID = "6a1e782e9ba0ae3d7727a668"


def _reload_config(monkeypatch, base=None):
    """Reload config with a chosen OVERLEAF_BASE_URL so the module-level
    constant picks it up."""
    if base is None:
        monkeypatch.delenv("OVERLEAF_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("OVERLEAF_BASE_URL", base)
    import overleaf_mcp.config as cfg
    return importlib.reload(cfg)


def test_default_base(monkeypatch):
    cfg = _reload_config(monkeypatch, None)
    assert cfg.project_url(_PID) == f"https://www.overleaf.com/project/{_PID}"


def test_self_hosted_base(monkeypatch):
    cfg = _reload_config(monkeypatch, "https://overleaf.corp.example.com/")
    # Trailing slash on the base must not double up.
    assert cfg.project_url(_PID) == (
        f"https://overleaf.corp.example.com/project/{_PID}"
    )


@pytest.mark.parametrize("bad", ["", ".", "overleaf-project/", "ABC", "x" * 24])
def test_invalid_id_returns_empty(monkeypatch, bad):
    cfg = _reload_config(monkeypatch, None)
    assert cfg.project_url(bad) == ""


def test_project_tag_embeds_url(monkeypatch):
    """_project_tag (used by every write tool) appends the canonical URL so
    a single edit_file result is enough for the client to build a link."""
    _reload_config(monkeypatch, "https://overleaf.corp.example.com")
    import overleaf_mcp.git_client as gc
    importlib.reload(gc)
    # Skip the network dashboard lookup — we only care about the URL tail.
    monkeypatch.setattr(gc, "_resolve_project_name", lambda pid: None)
    project = gc.ProjectConfig(name="x", project_id=_PID, git_token="t")
    tag = gc._project_tag(project)
    assert f"https://overleaf.corp.example.com/project/{_PID}" in tag
    assert "6a1e7…a668" in tag


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
