"""Tests for the comment-thread tools.

Everything here fakes the network. The parts worth pinning are the ones a
live smoke test would not catch reliably: that resolve finds its doc id by
joining ``/threads`` against ``/ranges``, that a stale CSRF token gets one
retry while a dead cookie does not, and that no reply can go out unmarked
by accident.
"""

import sys
from pathlib import Path

import httpx
import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import overleaf_mcp.threads as th  # noqa: E402

_PID = "6a54229b474e151c96a26afb"
_TID = "6a54229b474e151c96a26b01"
_DID = "6a54229b474e151c96a26aff"

_THREADS_BODY = {
    _TID: {
        "messages": [
            {
                "content": "Needs a citation.",
                "timestamp": 1,
                "user": {"first_name": "A.", "last_name": "Reviewer"},
            }
        ]
    },
    "resolved-one": {
        "resolved": True,
        "resolved_at": "2026-08-01T00:00:00Z",
        "resolved_by_user": {"first_name": "Co", "last_name": "Author"},
        "messages": [{"content": "done", "timestamp": 2, "user": None}],
    },
}

_RANGES_BODY = [
    {
        "id": _DID,
        "ranges": {
            "changes": [],
            "comments": [
                {"id": "c1", "op": {"c": "a novel framework", "p": 142, "t": _TID}}
            ],
        },
    }
]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    """Give every test a live cookie, a known prefix, and empty caches."""
    monkeypatch.setenv("OVERLEAF_SESSION", "s%3Afake")
    monkeypatch.setenv("OVERLEAF_REVIEW_PREFIX", "[auto]")
    th._csrf_cache.clear()
    th._anchor_cache.clear()
    monkeypatch.setattr(th, "_csrf_token", lambda pid: "tok")


def _resp(status, json_body=None, url="https://www.overleaf.com/x"):
    return httpx.Response(
        status, json=json_body if json_body is not None else {},
        request=httpx.Request("GET", url),
    )


def _fake_get(monkeypatch, routes):
    """Route GETs by URL substring."""
    def _get(url, **kwargs):
        for frag, body in routes.items():
            if frag in url:
                return _resp(200, body, url)
        raise AssertionError(f"unexpected GET {url}")
    monkeypatch.setattr(th.httpx, "get", _get)


# ── list_threads ──────────────────────────────────────────────────────

def test_list_threads_joins_anchors(monkeypatch):
    _fake_get(monkeypatch, {"/threads": _THREADS_BODY, "/ranges": _RANGES_BODY})
    out = th.list_threads(_PID)
    open_thread = next(t for t in out if t["id"] == _TID)
    assert open_thread["resolved"] is False
    assert open_thread["doc_id"] == _DID
    assert open_thread["quoted_text"] == "a novel framework"
    assert open_thread["position"] == 142
    assert open_thread["messages"][0]["user"] == "A. Reviewer"
    assert open_thread["messages"][0]["content"] == "Needs a citation."


def test_list_threads_can_exclude_resolved(monkeypatch):
    _fake_get(monkeypatch, {"/threads": _THREADS_BODY, "/ranges": _RANGES_BODY})
    assert [t["id"] for t in th.list_threads(_PID, include_resolved=False)] == [_TID]
    assert len(th.list_threads(_PID, include_resolved=True)) == 2


def test_resolved_thread_reports_its_resolver(monkeypatch):
    _fake_get(monkeypatch, {"/threads": _THREADS_BODY, "/ranges": _RANGES_BODY})
    resolved = next(t for t in th.list_threads(_PID) if t["id"] == "resolved-one")
    assert resolved["resolved"] is True
    assert resolved["resolved_by"] == "Co Author"


def test_list_threads_survives_a_missing_ranges_endpoint(monkeypatch):
    """A thread with no anchor is still worth listing — replying needs no doc id."""
    def _get(url, **kwargs):
        if "/threads" in url:
            return _resp(200, _THREADS_BODY, url)
        raise httpx.HTTPStatusError(
            "500", request=httpx.Request("GET", url), response=_resp(500, {}, url)
        )
    monkeypatch.setattr(th.httpx, "get", _get)
    out = th.list_threads(_PID)
    assert next(t for t in out if t["id"] == _TID)["doc_id"] is None


# ── reply ─────────────────────────────────────────────────────────────

def test_reply_is_prefixed_and_posts_content(monkeypatch):
    sent = {}

    def _post(url, json=None, headers=None, **kwargs):
        sent["url"], sent["json"], sent["headers"] = url, json, headers
        return _resp(200, {"id": "m1"}, url)

    monkeypatch.setattr(th.httpx, "post", _post)
    th.reply_to_thread(_PID, _TID, "Added Smith 2023.")
    assert sent["url"].endswith(f"/project/{_PID}/thread/{_TID}/messages")
    assert sent["json"] == {"content": "[auto] Added Smith 2023."}
    assert sent["headers"]["x-csrf-token"] == "tok"
    assert sent["headers"]["Cookie"] == "overleaf_session2=s%3Afake"


def test_reply_prefix_is_configurable_and_disablable(monkeypatch):
    seen = []
    monkeypatch.setattr(
        th.httpx, "post",
        lambda url, json=None, **kw: (seen.append(json["content"]), _resp(200, {}, url))[1],
    )
    monkeypatch.setenv("OVERLEAF_REVIEW_PREFIX", "[bot]")
    th.reply_to_thread(_PID, _TID, "x")
    monkeypatch.setenv("OVERLEAF_REVIEW_PREFIX", "")
    th.reply_to_thread(_PID, _TID, "x")
    assert seen == ["[bot] x", "x"]


def test_empty_reply_is_refused(monkeypatch):
    with pytest.raises(ValueError):
        th.reply_to_thread(_PID, _TID, "   ")


# ── resolve / reopen ──────────────────────────────────────────────────

def test_resolve_looks_up_the_doc_id(monkeypatch):
    _fake_get(monkeypatch, {"/ranges": _RANGES_BODY})
    posted = []
    monkeypatch.setattr(
        th.httpx, "post",
        lambda url, **kw: (posted.append(url), _resp(200, {}, url))[1],
    )
    out = th.resolve_thread(_PID, _TID)
    assert out == {"thread_id": _TID, "doc_id": _DID, "resolved": True}
    assert posted == [
        f"https://www.overleaf.com/project/{_PID}/doc/{_DID}/thread/{_TID}/resolve"
    ]


def test_reopen_hits_the_reopen_route(monkeypatch):
    _fake_get(monkeypatch, {"/ranges": _RANGES_BODY})
    posted = []
    monkeypatch.setattr(
        th.httpx, "post",
        lambda url, **kw: (posted.append(url), _resp(200, {}, url))[1],
    )
    assert th.reopen_thread(_PID, _TID)["resolved"] is False
    assert posted[0].endswith(f"/doc/{_DID}/thread/{_TID}/reopen")


def test_explicit_doc_id_skips_the_ranges_fetch(monkeypatch):
    monkeypatch.setattr(
        th.httpx, "get",
        lambda url, **kw: pytest.fail("should not need /ranges"),
    )
    monkeypatch.setattr(th.httpx, "post", lambda url, **kw: _resp(200, {}, url))
    assert th.resolve_thread(_PID, _TID, doc_id=_DID)["doc_id"] == _DID


def test_unanchored_thread_explains_itself(monkeypatch):
    _fake_get(monkeypatch, {"/ranges": []})
    monkeypatch.setattr(th.httpx, "post", lambda url, **kw: _resp(200, {}, url))
    with pytest.raises(RuntimeError, match="not anchored"):
        th.resolve_thread(_PID, "orphan")


# ── auth ──────────────────────────────────────────────────────────────

def test_stale_csrf_is_retried_once_then_succeeds(monkeypatch):
    calls = []

    def _post(url, json=None, headers=None, **kwargs):
        calls.append(headers["x-csrf-token"])
        if len(calls) == 1:
            return _resp(403, {}, url)
        return _resp(200, {"id": "m1"}, url)

    tokens = iter(["stale", "fresh"])
    monkeypatch.setattr(th, "_csrf_token", lambda pid: next(tokens))
    monkeypatch.setattr(th.httpx, "post", _post)
    th.reply_to_thread(_PID, _TID, "x")
    assert calls == ["stale", "fresh"]


def test_a_dead_cookie_returns_auth_expired_not_a_stack_trace(monkeypatch):
    monkeypatch.setattr(th.httpx, "post", lambda url, **kw: _resp(403, {}, url))
    with pytest.raises(th.AuthExpired) as e:
        th.reply_to_thread(_PID, _TID, "x")
    assert str(e.value).startswith("AUTH_EXPIRED")
    assert "overleaf_session2" in str(e.value)


def test_401_on_a_read_is_auth_expired(monkeypatch):
    monkeypatch.setattr(th.httpx, "get", lambda url, **kw: _resp(401, {}, url))
    with pytest.raises(th.AuthExpired):
        th.list_threads(_PID)


def test_a_signed_out_project_page_is_auth_expired(monkeypatch):
    """compile._csrf_token raises when the meta tag is gone — that is a dead cookie."""
    def _boom(pid):
        raise RuntimeError("Cannot find CSRF token — session cookie may have expired.")
    monkeypatch.setattr(th, "_csrf_token", _boom)
    monkeypatch.setattr(th.httpx, "post", lambda url, **kw: _resp(200, {}, url))
    with pytest.raises(th.AuthExpired):
        th.reply_to_thread(_PID, _TID, "x")


def test_auth_expired_is_logged_at_error(monkeypatch, caplog):
    monkeypatch.setattr(th.httpx, "post", lambda url, **kw: _resp(403, {}, url))
    with caplog.at_level("ERROR", logger="overleaf-mcp"):
        with pytest.raises(th.AuthExpired):
            th.reply_to_thread(_PID, _TID, "x")
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_csrf_is_fetched_once_per_project(monkeypatch):
    n = []
    monkeypatch.setattr(th, "_csrf_token", lambda pid: (n.append(pid), "tok")[1])
    monkeypatch.setattr(th.httpx, "post", lambda url, **kw: _resp(200, {}, url))
    th.reply_to_thread(_PID, _TID, "a")
    th.reply_to_thread(_PID, _TID, "b")
    assert len(n) == 1
