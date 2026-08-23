"""Git-based client for Overleaf project operations.

Handles clone, pull, push, diff, history, and file I/O through the
Overleaf Git bridge (``git.overleaf.com``).
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import shutil
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, TypeVar

from git import GitCommandError, PushInfo, Repo

from .config import (
    DIFF_CONTEXT_LINES,
    DIFF_MAX_OUTPUT_CHARS,
    HISTORY_LIMIT_DEFAULT,
    HISTORY_LIMIT_MAX,
    OVERLEAF_GIT_HOST,
    TEMP_DIR,
    ProjectConfig,
    project_url,
)
from .metadata import write_metadata

logger = logging.getLogger("overleaf-mcp")

# Per-project lock to prevent concurrent git operations on the same repo
# Reentrant locks so that high-level write tools can hold the lock across
# their full ``ensure_repo \u2192 add \u2192 commit \u2192 push`` sequence and still
# call into ``ensure_repo`` (which also acquires the lock) without
# deadlocking. See :func:`_with_project_lock`.
_project_locks: dict[str, threading.RLock] = {}
_locks_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Pull throttling — skip the ``git pull`` inside ``ensure_repo`` if we have
# pulled this repo within the last ``_PULL_TTL`` seconds. Set to 0 to disable.
# Configurable via OVERLEAF_PULL_TTL (seconds). Default: 30s.
# ---------------------------------------------------------------------------
_PULL_TTL = max(0, int(os.environ.get("OVERLEAF_PULL_TTL", "30")))
_last_pull_at: dict[str, float] = {}

# Push retry parameters for transient Overleaf git-bridge errors (flaky 5xx).
_PUSH_RETRIES = max(0, int(os.environ.get("OVERLEAF_PUSH_RETRIES", "2")))
_PUSH_BACKOFF_BASE = float(os.environ.get("OVERLEAF_PUSH_BACKOFF", "0.75"))

# How many times a write re-derives itself against a moved remote before
# giving up. See :func:`_write_and_push`.
_WRITE_ATTEMPTS = max(1, int(os.environ.get("OVERLEAF_WRITE_ATTEMPTS", "3")))


def _get_project_lock(project_id: str) -> threading.RLock:
    """Get or create a re-entrant threading lock for a specific project."""
    with _locks_lock:
        if project_id not in _project_locks:
            _project_locks[project_id] = threading.RLock()
        return _project_locks[project_id]


# ---------------------------------------------------------------------------
# Project identity helpers — used to build human-readable return messages
# so callers can tell *which* Overleaf project a write/read touched. Every
# write tool returns e.g. "✅ Created 'acl.sty' in project [My Paper] (69f21…cca7)"
# instead of a bare "Created and pushed 'acl.sty'". The project name is
# resolved once (and cached per-process) via the web dashboard when the
# session cookie is available; otherwise we fall back to the short ID only.
# ---------------------------------------------------------------------------
_project_name_cache: dict[str, str] = {}
_project_name_cache_lock = threading.Lock()


def _short_id(project_id: str) -> str:
    """Return a compact form of the 24-hex project ID like ``69f21…cca7``."""
    if not project_id or len(project_id) < 10:
        return project_id or "?"
    return f"{project_id[:5]}…{project_id[-4:]}"


def _resolve_project_name(project_id: str) -> str | None:
    """Try to resolve the human-readable project name via the web dashboard.

    Returns None when the session cookie is not configured or the lookup
    fails — callers should fall back to the short ID. Results are cached
    per process to avoid hitting the dashboard on every write.
    """
    if not project_id:
        return None
    with _project_name_cache_lock:
        if project_id in _project_name_cache:
            return _project_name_cache[project_id] or None
    try:
        # Import lazily — compile extras (httpx/bs4) are optional and we
        # want the git-only install to keep working.
        from . import compile as _compile_mod  # type: ignore
        from .credentials import get_session
        if not get_session():
            return None
        projects = _compile_mod.list_projects_web()
    except Exception as e:
        logger.debug("project name lookup skipped for %s: %s", project_id, e)
        with _project_name_cache_lock:
            _project_name_cache[project_id] = ""  # negative-cache to avoid retry storms
        return None
    name: str | None = None
    for p in projects or []:
        if p.get("id") == project_id:
            name = p.get("name")
            break
    with _project_name_cache_lock:
        _project_name_cache[project_id] = name or ""
    return name


def _project_tag(project: ProjectConfig) -> str:
    """Build a human-readable ``[Name] (short…id) <url>`` tag for a project.

    Falls back to ``(short…id) <url>`` if the natural name cannot be
    resolved. Used in the return messages of write operations so users can
    see at a glance *which* Overleaf project was modified — and click
    straight through to it.

    The canonical URL is derived from ``OVERLEAF_BASE_URL`` (see
    :func:`config.project_url`), so self-hosted deployments link correctly
    and MCP clients can turn the bare ID jumble into a real hyperlink
    instead of guessing the host.
    """
    sid = _short_id(project.project_id)
    name = _resolve_project_name(project.project_id)
    url = project_url(project.project_id)
    head = f"[{name}] ({sid})" if name else f"({sid})"
    return f"{head} {url}" if url else head


def _repo_path(project_id: str) -> Path:
    return Path(TEMP_DIR) / project_id


def _git_url(project: ProjectConfig) -> str:
    return f"https://git:{project.git_token}@{OVERLEAF_GIT_HOST}/{project.project_id}"


def _config_git_user(repo: Repo) -> None:
    """Ensure git user.name and user.email are configured."""
    try:
        repo.config_reader().get_value("user", "name")
    except Exception:
        name = os.environ.get("OVERLEAF_GIT_AUTHOR_NAME", "Overleaf MCP")
        email = os.environ.get("OVERLEAF_GIT_AUTHOR_EMAIL", "mcp@overleaf.local")
        with repo.config_writer() as cw:
            cw.set_value("user", "name", name)
            cw.set_value("user", "email", email)

    # Always pin a reconcile strategy. Without this, ``git pull`` aborts with
    # "Need to specify how to reconcile divergent branches" the moment the
    # Overleaf side gains a commit we do not have, which leaves the clone
    # permanently diverged and silently serving stale content to readers.
    try:
        with repo.config_writer() as cw:
            cw.set_value("pull", "rebase", "true")
    except Exception as e:  # pragma: no cover - best effort
        logger.warning("could not set pull.rebase: %s", e)


def validate_path(base: Path, target: str) -> Path:
    """Ensure *target* doesn't escape the repo root.

    Uses ``Path.relative_to`` (Python 3.9+) so that sibling directories with
    a shared prefix (``/tmp/foo`` vs ``/tmp/foobar``) cannot slip through a
    bare ``startswith`` check.
    """
    base_resolved = base.resolve()
    resolved = (base / target).resolve()
    try:
        resolved.relative_to(base_resolved)
    except ValueError as e:
        raise ValueError(f"Path '{target}' escapes repository root") from e
    return resolved


T = TypeVar("T")


def _retry_push(op: Callable[[], T], what: str) -> T:
    """Run ``op`` with exponential backoff on GitCommandError.

    The Overleaf git bridge occasionally drops pushes with 5xx responses
    under load. Retrying with a small jittered backoff fixes most of them.
    """
    last_exc: Exception | None = None
    attempts = 1 + _PUSH_RETRIES
    for i in range(attempts):
        try:
            return op()
        except GitCommandError as e:
            last_exc = e
            if i == attempts - 1:
                break
            delay = _PUSH_BACKOFF_BASE * (2 ** i) + random.uniform(0, 0.25)
            logger.warning(
                "git %s failed (attempt %d/%d): %s — retrying in %.2fs",
                what, i + 1, attempts, e, delay,
            )
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc


class PushRejected(Exception):
    """The remote refused our refs — normally because Overleaf moved ahead.

    Deliberately NOT a ``GitCommandError`` so :func:`_retry_push` does not
    burn its backoff budget retrying a rejection that will never succeed on
    its own. Transient 5xx failures stay retryable; rejections get rebased.
    """


def _push_once(repo: Repo) -> None:
    """One push attempt that actually reports whether the refs landed.

    ``Remote.push()`` does not raise on rejection: it returns a
    ``PushInfoList`` whose entries carry ERROR/REJECTED flags, with the
    exception parked on ``.error``. Reading the flags is the only reliable
    signal, because the porcelain stderr GitPython surfaces ("failed to push
    some refs") does not contain the words "non-fast-forward" or "rejected".
    """
    infos = repo.remotes.origin.push()
    error = getattr(infos, "error", None)
    if error is None:
        return
    rejected_mask = PushInfo.REJECTED | PushInfo.REMOTE_REJECTED
    if any(getattr(i, "flags", 0) & rejected_mask for i in infos):
        raise PushRejected(str(error))
    raise error


class ContentConflict(Exception):
    """The file changed underneath a caller who claimed to know its contents."""


def content_sha(text: str) -> str:
    """Short stable digest of file contents, used for optimistic concurrency."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _reset_to_remote(repo: Repo) -> None:
    """Discard local commits and match origin exactly."""
    repo.git.fetch("origin")
    branch = repo.active_branch.name
    repo.git.reset("--hard", f"origin/{branch}")


def _write_and_push(
    project: ProjectConfig,
    file_path: str,
    apply_fn: Callable[[str | None], str],
    commit_message: str,
    what: str,
) -> None:
    """Apply a content transform, commit, push; re-derive it if the remote moved.

    A search-and-replace never needs a three-way merge. If Overleaf gained a
    commit while we were working, the push is rejected; instead of rebasing
    our commit onto content it was not written against, we throw the commit
    away, take the remote's version of the file, and run the SAME transform
    over it. Either the anchor is still present, in which case the edit lands
    cleanly, or it is gone, in which case the caller gets an error naming the
    anchor rather than a merge conflict. Text conflicts stop being a category
    of failure a caller has to reason about.
    """
    with _with_project_lock(project.project_id):
        for attempt in range(1, _WRITE_ATTEMPTS + 1):
            repo = ensure_repo(project, force_pull=True)
            rp = _repo_path(project.project_id)
            target = validate_path(rp, file_path)
            current = target.read_text(encoding="utf-8") if target.exists() else None

            try:
                new_content = apply_fn(current)
            except (ValueError, ContentConflict) as e:
                if attempt == 1:
                    raise
                raise type(e)(
                    f"{e}\n\n(The document changed on Overleaf while this write was in "
                    f"flight, and the edit no longer applies to the current text. "
                    f"Re-read '{file_path}' and reissue the edit.)"
                ) from e

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new_content, encoding="utf-8")

            _config_git_user(repo)
            repo.index.add([file_path])
            repo.index.commit(commit_message)

            try:
                _retry_push(lambda: _push_once(repo), what)
                return
            except PushRejected as e:
                if attempt == _WRITE_ATTEMPTS:
                    raise RuntimeError(
                        f"{what} was rejected {attempt} times running; Overleaf kept "
                        f"moving under us. Nothing was pushed: {e}"
                    ) from e
                logger.warning(
                    "%s rejected; discarding our commit and re-applying against the "
                    "updated remote (attempt %d/%d)",
                    what, attempt, _WRITE_ATTEMPTS,
                )
                _reset_to_remote(repo)


def _push_checked(repo: Repo, what: str) -> None:
    """Push and verify it landed, healing the Overleaf-UI race once.

    Without this, a rejected push looked exactly like a successful one: the
    tool answered "✅", the commit stayed in the local clone, and because the
    git-side readers read that clone, every later read echoed the caller's
    own unpushed text back at them.
    """
    try:
        _retry_push(lambda: _push_once(repo), what)
        return
    except PushRejected as e:
        logger.warning("%s rejected (%s); rebasing onto origin and retrying", what, e)

    try:
        repo.git.pull("--rebase", "origin")
    except GitCommandError as e:
        # Never leave the clone parked mid-rebase; a half-finished rebase
        # breaks every subsequent tool call on this project.
        try:
            repo.git.rebase("--abort")
        except Exception:
            pass
        raise RuntimeError(
            f"{what} was rejected and the rebase onto origin failed: {e}. "
            "Nothing was pushed and the clone was left clean; the project "
            "needs manual reconciliation."
        ) from e

    try:
        _retry_push(lambda: _push_once(repo), f"{what} (after rebase)")
    except PushRejected as e:
        raise RuntimeError(
            f"{what} still rejected after rebasing onto origin: {e}. "
            "Nothing was pushed."
        ) from e


def _pull_if_stale(repo: Repo, project_id: str, force: bool = False) -> None:
    """Pull, skipping it if we pulled within ``_PULL_TTL`` seconds.

    ``force`` bypasses the TTL. Writes always force, so an edit is derived
    from the current remote content rather than a cached snapshot.
    """
    now = time.monotonic()
    last = _last_pull_at.get(project_id, 0.0)
    if not force and _PULL_TTL > 0 and (now - last) < _PULL_TTL:
        return
    try:
        repo.remotes.origin.pull()
        _last_pull_at[project_id] = now
    except GitCommandError as e:
        logger.warning("git pull failed for %s: %s", project_id, e)


def _is_valid_repo(rp: Path) -> bool:
    """Return True iff ``rp`` exists AND contains a usable git repository.

    An ``rp.exists()`` check alone is insufficient: a previous ``clone``
    aborted mid-stream (OOM, netsplit, user ran ``find -delete`` on the
    cache, …) can leave an empty directory or one with a half-written
    ``.git/`` tree. Opening such a directory via ``Repo(rp)`` raises
    ``InvalidGitRepositoryError`` and the server has no way to self-heal.
    We detect the bad state here so ``ensure_repo`` can wipe and re-clone.
    """
    if not rp.exists():
        return False
    try:
        _ = Repo(rp).git_dir
        return True
    except Exception:
        return False


def ensure_repo(project: ProjectConfig, force_pull: bool = False) -> Repo:
    """Clone or pull the project repo. Thread-safe per project.

    The lock is held for the whole lifetime of the returned ``Repo`` use
    in ``ensure_repo``; higher-level write operations must also hold the
    SAME lock around their ``index.add → commit → push`` sequence to
    avoid racing for ``.git/refs/heads/master.lock``. See
    :func:`_with_project_lock`.
    """
    lock = _get_project_lock(project.project_id)
    with lock:
        rp = _repo_path(project.project_id)
        git_url = _git_url(project)

        # Heal half-cloned / externally-wiped cache dirs by nuking and
        # re-cloning. Without this recovery path a single interrupted
        # clone permanently bricks the cache for that project.
        if rp.exists() and not _is_valid_repo(rp):
            logger.warning(
                "cache dir %s is not a valid git repo — removing and re-cloning",
                rp,
            )
            try:
                shutil.rmtree(rp)
            except OSError as e:
                logger.error("could not remove corrupt cache dir %s: %s", rp, e)
                raise

        if rp.exists():
            repo = Repo(rp)
            _pull_if_stale(repo, project.project_id, force=force_pull)
            # Ensure metadata exists (e.g. for repos cloned by older versions)
            try:
                write_metadata(
                    rp,
                    project.project_id,
                    source="git",
                    add_git_exclude=True,
                )
            except Exception as e:  # never block the real git op
                logger.debug("metadata refresh skipped: %s", e)
            return repo

        rp.parent.mkdir(parents=True, exist_ok=True)
        logger.info("Cloning project %s …", project.project_id)
        repo = Repo.clone_from(git_url, rp)
        _last_pull_at[project.project_id] = time.monotonic()
        try:
            write_metadata(
                rp,
                project.project_id,
                source="git",
                add_git_exclude=True,
            )
        except Exception as e:
            logger.debug("metadata write skipped after clone: %s", e)
        return repo


@contextmanager
def _with_project_lock(project_id: str):
    """Hold the per-project lock across a full write operation.

    Write tools (``create_file`` / ``edit_file`` / ``rewrite_file`` /
    ``upload_file`` / ``delete_file``) run three separate git commands —
    ``add``, ``commit``, ``push`` — that together take the per-repo lock
    on ``.git/refs/heads/master.lock``. If two concurrent callers race on
    the SAME ``project_id``, one of them will observe a ``.lock could
    not be obtained`` error (this reliably reproduced during parallel
    ``upload_file`` storms). Holding this outer lock across the whole
    write keeps same-project writes serialised while different projects
    remain fully parallel.
    """
    lock = _get_project_lock(project_id)
    with lock:
        yield


# ---------------------------------------------------------------------------
# Read operations
# ---------------------------------------------------------------------------

def list_files(project: ProjectConfig, extension: str = "") -> list[str]:
    """List files in the project, optionally filtered by extension."""
    ensure_repo(project)
    rp = _repo_path(project.project_id)
    files = []
    for p in sorted(rp.rglob("*")):
        if p.is_file() and not any(part.startswith(".") for part in p.relative_to(rp).parts):
            if not extension or p.suffix == extension:
                files.append(str(p.relative_to(rp)))
    return files


def read_file(project: ProjectConfig, file_path: str) -> str:
    """Read a file from the project."""
    ensure_repo(project)
    rp = _repo_path(project.project_id)
    target = validate_path(rp, file_path)
    if not target.exists():
        raise FileNotFoundError(f"File '{file_path}' not found in project")
    return target.read_text(encoding="utf-8")


def list_history(
    project: ProjectConfig,
    limit: int | None = None,
    file_path: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict[str, Any]]:
    """Return git commit history."""
    repo = ensure_repo(project)
    n = min(limit or HISTORY_LIMIT_DEFAULT, HISTORY_LIMIT_MAX)

    kwargs: dict[str, Any] = {"max_count": n}
    if file_path:
        kwargs["paths"] = file_path
    if since:
        kwargs["since"] = since
    if until:
        kwargs["until"] = until

    commits = list(repo.iter_commits(**kwargs))
    results = []
    for c in commits:
        results.append(
            {
                "hash": c.hexsha,
                "short": c.hexsha[:8],
                "date": c.committed_datetime.strftime("%Y-%m-%d %H:%M:%S"),
                "author": f"{c.author.name} <{c.author.email}>",
                "message": c.message.strip()[:200],
            }
        )
    return results


def get_diff(
    project: ProjectConfig,
    from_ref: str | None = None,
    to_ref: str | None = None,
    file_path: str | None = None,
    context_lines: int | None = None,
    max_chars: int | None = None,
) -> dict[str, Any]:
    """Return a git diff."""
    repo = ensure_repo(project)
    ctx = max(0, min(context_lines or DIFF_CONTEXT_LINES, 10))
    limit = max(2000, max_chars or DIFF_MAX_OUTPUT_CHARS)

    args: list[str] = []
    fr = from_ref or "HEAD"
    if to_ref:
        args = [fr, to_ref]
    else:
        args = [fr]

    try:
        if file_path:
            diff = repo.git.diff(*args, "--", file_path, unified=ctx, no_color=True)
        else:
            diff = repo.git.diff(*args, unified=ctx, no_color=True)
    except GitCommandError as e:
        return {"diff": f"Error: {e}", "truncated": False}

    truncated = len(diff) > limit
    return {"diff": diff[:limit] if truncated else diff, "truncated": truncated}


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def create_file(
    project: ProjectConfig,
    file_path: str,
    content: str,
    commit_message: str | None = None,
) -> str:
    """Create a new file, commit and push."""
    with _with_project_lock(project.project_id):
        repo = ensure_repo(project)
        rp = _repo_path(project.project_id)
        target = validate_path(rp, file_path)

        if target.exists():
            raise FileExistsError(
                f"File '{file_path}' already exists. Use edit_file or rewrite_file."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        _config_git_user(repo)
        repo.index.add([file_path])
        repo.index.commit(commit_message or f"Add {file_path}")
        _push_checked(repo, f"push(create {file_path})")
        return f"✅ Created '{file_path}' in project {_project_tag(project)}"


def edit_file(
    project: ProjectConfig,
    file_path: str,
    old_string: str,
    new_string: str,
    commit_message: str | None = None,
) -> str:
    """Surgical search-and-replace edit, commit and push.

    The replacement is re-derived against the live remote content if Overleaf
    moves while the write is in flight, so this never produces a merge
    conflict: it either applies or reports that the anchor is gone.
    """

    def apply(content: str | None) -> str:
        if content is None:
            raise FileNotFoundError(f"File '{file_path}' not found")
        if old_string not in content:
            preview = content[:500] + ("…" if len(content) > 500 else "")
            raise ValueError(
                f"old_string not found in '{file_path}'. Preview:\n{preview}"
            )
        count = content.count(old_string)
        if count > 1:
            raise ValueError(
                f"old_string appears {count} times in '{file_path}'. "
                "Make it more specific to match exactly once."
            )
        return content.replace(old_string, new_string, 1)

    _write_and_push(
        project,
        file_path,
        apply,
        commit_message or f"Edit {file_path}",
        f"push(edit {file_path})",
    )
    return f"✅ Edited '{file_path}' in project {_project_tag(project)}"


def rewrite_file(
    project: ProjectConfig,
    file_path: str,
    content: str,
    commit_message: str | None = None,
    expected_sha: str | None = None,
) -> str:
    """Replace entire file contents, commit and push.

    A whole-file replacement cannot be re-derived the way an anchored edit
    can: the caller composed *content* against some specific version of the
    file, and if the file has moved on, blindly writing it silently discards
    whoever edited in between. ``expected_sha`` is the optimistic-concurrency
    guard — pass the digest shown in ``read_file``'s header and the write is
    refused if the file no longer matches.
    """

    def apply(current: str | None) -> str:
        if current is None:
            raise FileNotFoundError(
                f"File '{file_path}' not found. Use create_file instead."
            )
        if expected_sha:
            actual = content_sha(current)
            if not actual.startswith(expected_sha.strip().lower()[:12]):
                raise ContentConflict(
                    f"'{file_path}' has changed since you read it "
                    f"(expected sha {expected_sha}, found {actual}). "
                    "Someone edited it in the meantime; re-read the file, "
                    "rebuild your replacement on top of the current text, and "
                    "reissue the rewrite. Nothing was written."
                )
        return content

    _write_and_push(
        project,
        file_path,
        apply,
        commit_message or f"Rewrite {file_path}",
        f"push(rewrite {file_path})",
    )
    return (
        f"✅ Rewrote '{file_path}' in project {_project_tag(project)} "
        f"(sha {content_sha(content)})"
    )


def upload_file(
    project: ProjectConfig,
    file_path: str,
    source_path: str,
    commit_message: str | None = None,
    overwrite: bool = False,
) -> str:
    """Copy a local (possibly binary) file into the repo, commit and push.

    Unlike ``create_file`` / ``rewrite_file``, this path is byte-safe:
    the source is read and written as raw bytes, making it suitable for
    images (PNG/JPG), PDFs, fonts, and other non-text assets that must
    not be decoded as UTF-8.

    Args:
        project: ProjectConfig for the target Overleaf project.
        file_path: Destination path inside the repo (e.g. ``'figures/cat.png'``).
        source_path: Local filesystem path to the file to upload.
        commit_message: Optional commit message.
        overwrite: If False (default), fail when ``file_path`` already
            exists in the repo. Set True to replace it.

    Returns:
        Human-readable confirmation string.
    """
    src = Path(source_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"Source file '{source_path}' does not exist")
    if not src.is_file():
        raise ValueError(f"Source path '{source_path}' is not a regular file")

    with _with_project_lock(project.project_id):
        repo = ensure_repo(project)
        rp = _repo_path(project.project_id)
        target = validate_path(rp, file_path)

        existed = target.exists()
        if existed and not overwrite:
            raise FileExistsError(
                f"File '{file_path}' already exists. Pass overwrite=true to replace it."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        data = src.read_bytes()
        target.write_bytes(data)

        _config_git_user(repo)
        repo.index.add([file_path])
        repo.index.commit(commit_message or f"Upload {file_path}")
        _push_checked(repo, f"push(upload {file_path})")
        size_kb = len(data) / 1024
        verb = "Replaced" if existed else "Uploaded"
        return (
            f"✅ {verb} '{file_path}' ({size_kb:.1f} KB) in project "
            f"{_project_tag(project)}"
        )


def delete_file(
    project: ProjectConfig,
    file_path: str,
    commit_message: str | None = None,
) -> str:
    """Delete a file, commit and push."""
    with _with_project_lock(project.project_id):
        repo = ensure_repo(project)
        rp = _repo_path(project.project_id)
        target = validate_path(rp, file_path)

        if not target.exists():
            raise FileNotFoundError(f"File '{file_path}' not found")

        _config_git_user(repo)
        repo.index.remove([file_path])
        target.unlink()
        repo.index.commit(commit_message or f"Delete {file_path}")
        _push_checked(repo, f"push(delete {file_path})")
        return f"✅ Deleted '{file_path}' from project {_project_tag(project)}"


def sync_project(project: ProjectConfig) -> str:
    """Pull latest changes from Overleaf (force-bypasses the pull TTL)."""
    rp = _repo_path(project.project_id)

    # Route through ensure_repo so a corrupt/half-cloned cache dir is
    # self-healed (wiped + re-cloned) instead of raising
    # InvalidGitRepositoryError here. ensure_repo also takes the
    # per-project lock, which we re-acquire below to safely pull.
    if not rp.exists() or not _is_valid_repo(rp):
        ensure_repo(project)
        return f"Cloned project '{project.name}'"

    with _with_project_lock(project.project_id):
        try:
            repo = Repo(rp)
        except Exception as e:
            # Shouldn't happen after _is_valid_repo, but heal just in case.
            logger.warning(
                "sync_project: Repo(%s) failed (%s) — forcing re-clone via ensure_repo",
                rp, e,
            )
            ensure_repo(project)
            return f"Re-cloned project '{project.name}' after detecting corrupt cache"

        if repo.is_dirty():
            return "Warning: uncommitted local changes. Commit or discard them before syncing."

        try:
            repo.remotes.origin.pull()
            _last_pull_at[project.project_id] = time.monotonic()
            return f"Synced project '{project.name}' with Overleaf"
        except GitCommandError as e:
            logger.warning("sync_project: git pull failed for %s: %s", project.project_id, e)
            return f"Error syncing: {e}"
