"""Configuration management for Overleaf MCP Server."""

from __future__ import annotations

import logging
import os
import re

from pydantic import BaseModel

from .credentials import get_git_token

# Overleaf project IDs are 24-character lowercase hex strings (MongoDB ObjectIDs)
_PROJECT_ID_RE = re.compile(r"^[0-9a-f]{24}$")

logger = logging.getLogger("overleaf-mcp")

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
# Resolve TEMP_DIR to an *absolute* path at import time so the cache
# location is independent of the process's current working directory.
# A relative default like ``./overleaf_cache`` would silently redirect to
# a different filesystem location every time another library (or the host
# MCP runtime) chdirs — historically this caused deeply-nested
# ``overleaf_cache/<id>/overleaf_cache/<id>/…`` duplicates, stale ``Repo``
# handles, and cryptic ``FileExistsError: [Errno 17] File exists: '.'``
# bubbling up from GitPython. Absolutising once at startup eliminates the
# whole class of cwd-coupled bugs.
TEMP_DIR = os.path.abspath(
    os.path.expanduser(os.environ.get("OVERLEAF_TEMP_DIR", "./overleaf_cache"))
)
OVERLEAF_BASE_URL = os.environ.get("OVERLEAF_BASE_URL", "https://www.overleaf.com")
OVERLEAF_GIT_HOST = os.environ.get("OVERLEAF_GIT_HOST", "git.overleaf.com")

# History / diff limits
HISTORY_LIMIT_DEFAULT = int(os.environ.get("HISTORY_LIMIT_DEFAULT", "20"))
HISTORY_LIMIT_MAX = int(os.environ.get("HISTORY_LIMIT_MAX", "200"))
DIFF_CONTEXT_LINES = int(os.environ.get("DIFF_CONTEXT_LINES", "3"))
DIFF_MAX_OUTPUT_CHARS = int(os.environ.get("DIFF_MAX_OUTPUT_CHARS", "120000"))


def project_url(project_id: str) -> str:
    """Return the canonical web URL for a project on this deployment.

    Uses ``OVERLEAF_BASE_URL`` so self-hosted Overleaf instances produce
    correct links (the public default is ``https://www.overleaf.com``).
    Returns '' for a falsy/invalid id so callers can omit the link rather
    than emit a broken ``…/project/`` URL.
    """
    if not project_id or not _PROJECT_ID_RE.match(project_id):
        return ""
    return f"{OVERLEAF_BASE_URL.rstrip('/')}/project/{project_id}"


class ProjectConfig(BaseModel):
    """Configuration for a single Overleaf project."""

    name: str
    project_id: str
    git_token: str


def _get_git_token() -> str:
    """Get the account-level git token from the OVERLEAF_GIT_TOKEN env var."""
    token = get_git_token()
    if not token:
        raise ValueError(
            "Git token not configured. Set the OVERLEAF_GIT_TOKEN environment "
            "variable.  Generate a token at: "
            "https://www.overleaf.com/user/settings → Git Integration → Create Token"
        )
    return token


def get_project(project_id: str) -> ProjectConfig:
    """Build a ProjectConfig for a given project ID.

    The git token is account-level — one token works for all projects.
    Just provide a project_id (from list_projects or the Overleaf URL).
    """
    if not project_id:
        raise ValueError(
            "project_id is required. "
            "Use list_projects to discover your projects, or copy it from the Overleaf URL."
        )
    if not _PROJECT_ID_RE.match(project_id):
        raise ValueError(
            f"Invalid project_id {project_id!r}. Overleaf project IDs are "
            "24-character lowercase hex strings (e.g. '692a83fb82feceb233c4b0e7'). "
            "This tool operates on the REMOTE Overleaf repo, not your local filesystem — "
            "a filesystem path like '.' or 'overleaf-project/' is NOT a project_id. "
            "These overleaf_* tools are ONLY for Overleaf projects — do not call them "
            "for generic local file I/O; use the standard read_files / grep_search tools "
            "for that. If you already have a checked-out copy of THIS Overleaf project "
            "locally, use read_files / grep_search on that local path instead. "
            "Otherwise, call list_projects to discover the correct 24-hex ID."
        )
    token = _get_git_token()
    return ProjectConfig(
        name=f"Project {project_id[:8]}…",
        project_id=project_id,
        git_token=token,
    )
