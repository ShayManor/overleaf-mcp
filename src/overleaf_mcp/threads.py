"""Comment threads: read, reply, resolve, reopen (session-cookie auth).

These are the *review-panel* comment threads, not the project-wide chat.
They require the ``compile`` optional dependency group (httpx + bs4) and
the ``OVERLEAF_SESSION`` environment variable, exactly like the compile
tools — which is why this module reuses ``compile._headers`` rather than
re-deriving the cookie header.

Endpoint shapes were taken from Overleaf's own review-panel frontend
(``services/web/frontend/js/features/review-panel/context/threads-context.tsx``
and ``hooks/use-project-ranges.ts``), not guessed::

    GET    /project/<pid>/threads
    GET    /project/<pid>/ranges
    POST   /project/<pid>/thread/<tid>/messages            {"content": …}
    POST   /project/<pid>/doc/<did>/thread/<tid>/resolve
    POST   /project/<pid>/doc/<did>/thread/<tid>/reopen

Note the asymmetry: replying needs only the thread id, but resolve and
reopen are routed *under the document* and need the doc id too. Nothing
in ``/threads`` says which document a thread is anchored to — that lives
in ``/ranges``, keyed by doc id with the thread id in ``op.t``. So this
module joins the two, and caches the resulting thread→doc map.

Creating a *new* comment on a text range is deliberately absent: that
binds a thread id to a document range over the realtime channel, not
REST (see ``addComment`` in threads-context.tsx, which POSTs the message
and then ``submitOp``s a comment operation over ShareJS).
"""

from __future__ import annotations

import logging

import httpx

from .compile import _headers, _csrf_token
from .config import OVERLEAF_BASE_URL
from .credentials import get_review_prefix

logger = logging.getLogger("overleaf-mcp")

AUTH_EXPIRED = "AUTH_EXPIRED"

_AUTH_EXPIRED_HELP = (
    f"{AUTH_EXPIRED}: the Overleaf session cookie is no longer valid.\n"
    "Refresh it: log into https://www.overleaf.com, press F12 → Application "
    "→ Cookies → https://www.overleaf.com → copy the Value of "
    "`overleaf_session2` (starts with `s%3A…`), put it in OVERLEAF_SESSION, "
    "and restart the MCP server."
)


class AuthExpired(RuntimeError):
    """Raised when the session cookie is rejected and a CSRF retry did not help."""

    def __init__(self, detail: str = "") -> None:
        msg = _AUTH_EXPIRED_HELP
        if detail:
            msg = f"{msg}\n(detail: {detail})"
        super().__init__(msg)


# ---------------------------------------------------------------------------
# CSRF token, cached per project for the process lifetime
# ---------------------------------------------------------------------------
_csrf_cache: dict[str, str] = {}


def _csrf(project_id: str, *, refresh: bool = False) -> str:
    """Return the project's CSRF token, fetching it at most once per project.

    A 403 on a POST means the cached token went stale (the project page was
    re-rendered with a new one), so callers pass ``refresh=True`` to force a
    refetch before their single retry.
    """
    if refresh:
        _csrf_cache.pop(project_id, None)
    token = _csrf_cache.get(project_id)
    if token:
        return token
    try:
        token = _csrf_token(project_id)
    except httpx.HTTPStatusError as e:
        if e.response.status_code in (401, 403):
            logger.error("Overleaf auth rejected while fetching CSRF: %s", e)
            raise AuthExpired(str(e)) from e
        raise
    except RuntimeError as e:
        # compile._csrf_token raises this when the ol-csrfToken meta tag is
        # absent, which is what a signed-out project page looks like.
        if "CSRF token" in str(e):
            logger.error("Overleaf session appears signed out: %s", e)
            raise AuthExpired(str(e)) from e
        raise
    if not token:
        raise AuthExpired("empty ol-csrfToken")
    _csrf_cache[project_id] = token
    return token


def _json_headers(project_id: str, *, refresh_csrf: bool = False) -> dict[str, str]:
    return {
        **_headers(),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-csrf-token": _csrf(project_id, refresh=refresh_csrf),
    }


def _get(project_id: str, path: str) -> httpx.Response:
    """GET a JSON endpoint, mapping 401/403 to AuthExpired."""
    r = httpx.get(
        f"{OVERLEAF_BASE_URL}{path}",
        headers={**_headers(), "Accept": "application/json"},
        follow_redirects=True,
        timeout=30,
    )
    if r.status_code in (401, 403):
        logger.error("Overleaf auth rejected on GET %s (HTTP %s)", path, r.status_code)
        raise AuthExpired(f"GET {path} → HTTP {r.status_code}")
    r.raise_for_status()
    return r


def _post(project_id: str, path: str, payload: dict | None = None) -> httpx.Response:
    """POST to a web endpoint with cookie + CSRF, retrying once on 403.

    The retry exists because the cached CSRF token can go stale while the
    process keeps running. One refetch-and-retry distinguishes a stale token
    (succeeds) from a dead cookie (fails again → AUTH_EXPIRED).
    """
    for attempt in (0, 1):
        r = httpx.post(
            f"{OVERLEAF_BASE_URL}{path}",
            json=payload if payload is not None else {},
            headers=_json_headers(project_id, refresh_csrf=bool(attempt)),
            timeout=30,
        )
        if r.status_code == 403 and attempt == 0:
            logger.warning("POST %s → 403, refreshing CSRF and retrying once", path)
            continue
        if r.status_code in (401, 403):
            logger.error(
                "Overleaf auth rejected on POST %s (HTTP %s) after CSRF retry",
                path,
                r.status_code,
            )
            raise AuthExpired(f"POST {path} → HTTP {r.status_code}")
        r.raise_for_status()
        return r
    raise AssertionError("unreachable")


# ---------------------------------------------------------------------------
# thread → doc mapping (from /ranges)
# ---------------------------------------------------------------------------
_anchor_cache: dict[str, dict[str, dict]] = {}


def _fetch_anchors(project_id: str) -> dict[str, dict]:
    """Map thread_id → {doc_id, quoted_text, position} from the project ranges.

    ``/ranges`` returns one entry per document; each comment range carries the
    thread id in ``op.t`` and the commented-on text in ``op.c``.
    """
    data = _get(project_id, f"/project/{project_id}/ranges").json()
    anchors: dict[str, dict] = {}
    for doc in data or []:
        doc_id = doc.get("id")
        for comment in (doc.get("ranges") or {}).get("comments") or []:
            op = comment.get("op") or {}
            thread_id = op.get("t") or comment.get("id")
            if not thread_id:
                continue
            anchors[thread_id] = {
                "doc_id": doc_id,
                "quoted_text": op.get("c"),
                "position": op.get("p"),
            }
    _anchor_cache[project_id] = anchors
    return anchors


def _doc_id_for(project_id: str, thread_id: str) -> str:
    """Return the doc id a thread is anchored to, refetching once on a miss."""
    anchors = _anchor_cache.get(project_id)
    if anchors is None or thread_id not in anchors:
        anchors = _fetch_anchors(project_id)
    entry = anchors.get(thread_id)
    if not entry or not entry.get("doc_id"):
        raise RuntimeError(
            f"Thread {thread_id} is not anchored to any document in project "
            f"{project_id}. Resolve/reopen are routed under the document, so "
            "the doc id is required. The thread may already be deleted, or its "
            "anchor may have been orphaned by an edit — call list_threads to "
            "see the current anchors."
        )
    return entry["doc_id"]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def list_threads(project_id: str, include_resolved: bool = True) -> list[dict]:
    """Return every comment thread in the project, newest message last.

    Each entry has ``id``, ``resolved``, ``messages`` (``user``, ``content``,
    ``timestamp``) and, when the thread is still anchored, ``doc_id``,
    ``quoted_text`` and ``position``.
    """
    raw = _get(project_id, f"/project/{project_id}/threads").json() or {}
    try:
        anchors = _fetch_anchors(project_id)
    except httpx.HTTPStatusError as e:
        # An unanchored listing is still useful for replying; only resolve
        # actually needs the doc id.
        logger.warning("Could not fetch ranges for %s: %s", project_id, e)
        anchors = {}

    threads: list[dict] = []
    for thread_id, thread in raw.items():
        resolved = bool(thread.get("resolved"))
        if resolved and not include_resolved:
            continue
        anchor = anchors.get(thread_id, {})
        threads.append(
            {
                "id": thread_id,
                "resolved": resolved,
                "resolved_at": thread.get("resolved_at"),
                "resolved_by": _user_name(thread.get("resolved_by_user")),
                "doc_id": anchor.get("doc_id"),
                "quoted_text": anchor.get("quoted_text"),
                "position": anchor.get("position"),
                "messages": [
                    {
                        "user": _user_name(m.get("user")),
                        "content": m.get("content", ""),
                        "timestamp": m.get("timestamp"),
                    }
                    for m in thread.get("messages") or []
                ],
            }
        )
    return threads


def _user_name(user: dict | None) -> str:
    """Best-effort display name for a thread participant."""
    if not user:
        return "unknown"
    name = " ".join(
        p for p in (user.get("first_name"), user.get("last_name")) if p
    ).strip()
    return name or user.get("email") or "unknown"


def reply_to_thread(project_id: str, thread_id: str, content: str) -> dict:
    """Post a reply on an existing thread. Returns the created message."""
    if not content or not content.strip():
        raise ValueError("Reply content cannot be empty.")
    prefix = get_review_prefix()
    body = f"{prefix} {content}".strip() if prefix else content
    r = _post(
        project_id,
        f"/project/{project_id}/thread/{thread_id}/messages",
        {"content": body},
    )
    try:
        return r.json()
    except Exception:
        return {"status": r.status_code, "thread_id": thread_id, "content": body}


def resolve_thread(project_id: str, thread_id: str, doc_id: str | None = None) -> dict:
    """Mark a thread resolved. ``doc_id`` is looked up from /ranges if omitted."""
    doc_id = doc_id or _doc_id_for(project_id, thread_id)
    _post(project_id, f"/project/{project_id}/doc/{doc_id}/thread/{thread_id}/resolve")
    return {"thread_id": thread_id, "doc_id": doc_id, "resolved": True}


def reopen_thread(project_id: str, thread_id: str, doc_id: str | None = None) -> dict:
    """Reopen a resolved thread. ``doc_id`` is looked up from /ranges if omitted."""
    doc_id = doc_id or _doc_id_for(project_id, thread_id)
    _post(project_id, f"/project/{project_id}/doc/{doc_id}/thread/{thread_id}/reopen")
    return {"thread_id": thread_id, "doc_id": doc_id, "resolved": False}
