"""Local-copy metadata sidecar for Overleaf projects.

When a project is downloaded (either via ``download_source`` or via the
git clone performed by ``git_client.ensure_repo``) we drop a small JSON
file at the root of the local copy that records:

    - project_id       (24-char Overleaf ID)
    - name             (human project title, best-effort)
    - overleaf_url     (https://www.overleaf.com/project/<id>)
    - source           ("git" | "zip")
    - downloaded_at    (UTC ISO-8601 timestamp)

This lets any tool (the assistant included) unambiguously identify which
Overleaf project a local working directory corresponds to — no more
guessing by file names. The file is called ``.overleaf-project.json``
and lives at the root of the extracted / cloned project.

For git-based copies we also add the file to ``.git/info/exclude`` so
it never gets committed back to Overleaf.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from .config import OVERLEAF_BASE_URL

logger = logging.getLogger("overleaf-mcp")

METADATA_FILENAME = ".overleaf-project.json"


def _build_payload(
    project_id: str,
    source: str,
    name: str | None = None,
) -> dict[str, str]:
    """Build the metadata dict."""
    return {
        "project_id": project_id,
        "name": name or "",
        "overleaf_url": f"{OVERLEAF_BASE_URL.rstrip('/')}/project/{project_id}",
        "source": source,
        "downloaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "_note": (
            "This file was written by overleaf-mcp to identify the remote "
            "Overleaf project this local copy corresponds to. Safe to keep "
            "in place; it is ignored by git so it will not be pushed back "
            "to Overleaf."
        ),
    }


def _resolve_name(project_id: str) -> str | None:
    """Best-effort lookup of the human project name via the web dashboard.

    Returns None if the compile extra isn't installed or no session cookie
    is configured — callers should tolerate a missing name.
    """
    try:
        from . import compile as _compile_mod  # local import to avoid cycle
        from .credentials import get_session
    except Exception:
        return None
    if not get_session():
        return None
    try:
        projects = _compile_mod.list_projects_web()
    except Exception as e:
        logger.debug("metadata: could not resolve project name: %s", e)
        return None
    for p in projects:
        if p.get("id") == project_id:
            return p.get("name")
    return None


def write_metadata(
    root: str | os.PathLike[str],
    project_id: str,
    source: str,
    *,
    name: str | None = None,
    add_git_exclude: bool = False,
    refresh: bool = False,
) -> Path:
    """Write the metadata sidecar to ``root/.overleaf-project.json``.

    Parameters
    ----------
    root:
        Local directory that holds the project copy.
    project_id:
        24-hex Overleaf project ID.
    source:
        How this copy was obtained — ``"git"`` or ``"zip"``.
    name:
        Optional human project name. If omitted, we try to resolve it via
        ``list_projects_web`` (requires session cookie).
    add_git_exclude:
        If True, append the metadata filename to ``.git/info/exclude`` so
        the file is never tracked by git. Use this for git-backed copies.
    refresh:
        If False (default), skip writing when an existing sidecar already
        has the same project_id and a non-empty name — avoids an HTTP
        lookup on every git pull. Pass True to force a rewrite.

    Returns the path that was written (or would have been written).
    """
    root_p = Path(root)
    root_p.mkdir(parents=True, exist_ok=True)
    target = root_p / METADATA_FILENAME

    # Idempotency check — skip if we already have good metadata
    if not refresh and target.exists():
        existing = read_metadata(root_p)
        if (
            existing
            and existing.get("project_id") == project_id
            and existing.get("name")
        ):
            if add_git_exclude:
                _add_git_exclude(root_p)
            return target

    if not name:
        name = _resolve_name(project_id)

    payload = _build_payload(project_id, source, name=name)
    try:
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write %s: %s", target, e)
        return target

    if add_git_exclude:
        _add_git_exclude(root_p)

    return target


def _add_git_exclude(root_p: Path) -> None:
    """Append METADATA_FILENAME to .git/info/exclude (idempotent)."""
    exclude = root_p / ".git" / "info" / "exclude"
    try:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if METADATA_FILENAME not in current.splitlines():
            with exclude.open("a", encoding="utf-8") as fh:
                if current and not current.endswith("\n"):
                    fh.write("\n")
                fh.write(f"{METADATA_FILENAME}\n")
    except OSError as e:
        logger.debug("metadata: could not update .git/info/exclude: %s", e)


def read_metadata(root: str | os.PathLike[str]) -> dict[str, str] | None:
    """Read the metadata sidecar from ``root`` if present, else None."""
    target = Path(root) / METADATA_FILENAME
    if not target.exists():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("metadata: could not read %s: %s", target, e)
        return None
