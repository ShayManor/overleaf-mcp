"""Tests for anchored comment creation.

The socket.io handshake itself can only be verified against production, so
what is pinned here is everything around it: the op shape, anchor resolution,
the ordering of REST-then-realtime, and the failure modes that would
otherwise create an orphan thread nobody can see.
"""

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import overleaf_mcp.realtime as rt  # noqa: E402

_PID = "6a88ed218242c22926e41c93"
_DID = "6a88ed218242c22926e41ca4"

_DOC = (
    "\\documentclass{article}\n"
    "\\begin{document}\n"
    "We evaluate under distribution shift.\n"
    "We report distribution shift again.\n"
    "\\end{document}\n"
)


class _FakeRealtime:
    """Stands in for a live socket.io session, recording what was sent."""

    instances: list = []

    def __init__(self, project_id):
        self.project_id = project_id
        self.root_doc_id = _DID
        self.ops = []
        self.joined = []
        _FakeRealtime.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def join_project(self):
        return {_DID: "main.tex", "otherdoc": "sections/intro.tex"}

    def join_doc(self, doc_id):
        self.joined.append(doc_id)
        return _DOC, 7

    def submit_comment_op(self, doc_id, anchor_text, position, thread_id, version):
        self.ops.append(
            {"doc_id": doc_id, "anchor": anchor_text, "pos": position,
             "thread_id": thread_id, "version": version}
        )


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    _FakeRealtime.instances.clear()
    monkeypatch.setenv("OVERLEAF_SESSION", "s%3Afake")
    monkeypatch.setattr(rt, "_Realtime", _FakeRealtime)
    monkeypatch.setattr(rt, "_HAS_WS", True)
    posted = []

    class _R:
        status_code = 200
        def raise_for_status(self): pass

    def _post(url, json=None, headers=None, **kw):
        posted.append({"url": url, "json": json, "headers": headers})
        return _R()

    monkeypatch.setattr(rt.httpx, "post", _post)
    import overleaf_mcp.threads as th
    monkeypatch.setattr(th, "_csrf", lambda pid, **kw: "tok")
    return posted


# ── thread ids ────────────────────────────────────────────────────────

def test_thread_ids_are_objectid_shaped_and_unique():
    ids = {rt.new_thread_id() for _ in range(50)}
    assert len(ids) == 50
    for i in ids:
        assert len(i) == 24
        int(i, 16)  # must be valid hex


# ── anchoring ─────────────────────────────────────────────────────────

def test_comment_anchors_at_the_first_match(_wire):
    out = rt.create_comment(_PID, "distribution shift", "Which shift?", file="main.tex")
    assert out["position"] == _DOC.find("distribution shift")
    assert out["doc_id"] == _DID
    assert out["file"] == "main.tex"
    assert out["quoted_text"] == "distribution shift"


def test_occurrence_selects_the_second_match(_wire):
    first = _DOC.find("distribution shift")
    out = rt.create_comment(_PID, "distribution shift", "here", file="main.tex",
                            occurrence=2)
    assert out["position"] == _DOC.find("distribution shift", first + 1)
    assert out["position"] != first


def test_missing_anchor_is_refused_before_any_thread_is_minted(_wire):
    with pytest.raises(ValueError, match="Could not find"):
        rt.create_comment(_PID, "no such text", "x", file="main.tex")
    assert _wire == [], "must not POST a message it cannot anchor"
    assert _FakeRealtime.instances[0].ops == []


def test_occurrence_beyond_the_matches_is_refused(_wire):
    with pytest.raises(ValueError, match="Could not find"):
        rt.create_comment(_PID, "distribution shift", "x", file="main.tex",
                          occurrence=3)


def test_unknown_file_lists_what_is_available(_wire):
    with pytest.raises(ValueError, match="sections/intro.tex"):
        rt.create_comment(_PID, "distribution shift", "x", file="nope.tex")


def test_omitting_file_uses_the_root_document(_wire):
    out = rt.create_comment(_PID, "distribution shift", "x")
    assert out["doc_id"] == _DID
    assert _FakeRealtime.instances[0].joined == [_DID]


# ── the two halves, in order ──────────────────────────────────────────

def test_rest_message_and_realtime_op_share_the_thread_id(_wire):
    out = rt.create_comment(_PID, "distribution shift", "Which shift?", file="main.tex")
    tid = out["thread_id"]
    assert _wire[0]["url"].endswith(f"/project/{_PID}/thread/{tid}/messages")
    assert _wire[0]["json"] == {"content": "Which shift?"}
    assert _wire[0]["headers"]["x-csrf-token"] == "tok"
    op = _FakeRealtime.instances[0].ops[0]
    assert op["thread_id"] == tid, "an id mismatch orphans the thread"


def test_the_op_carries_the_version_from_join_doc(_wire):
    rt.create_comment(_PID, "distribution shift", "x", file="main.tex")
    assert _FakeRealtime.instances[0].ops[0]["version"] == 7


def test_comment_is_never_left_unanchored(_wire):
    """A posted message with no op is a thread nobody can see."""
    rt.create_comment(_PID, "distribution shift", "x", file="main.tex")
    assert len(_wire) == 1 and len(_FakeRealtime.instances[0].ops) == 1


# ── input validation ──────────────────────────────────────────────────

@pytest.mark.parametrize("content", ["", "   "])
def test_empty_content_is_refused(_wire, content):
    with pytest.raises(ValueError):
        rt.create_comment(_PID, "distribution shift", content, file="main.tex")


def test_empty_anchor_is_refused(_wire):
    with pytest.raises(ValueError, match="anchor_text is required"):
        rt.create_comment(_PID, "", "x", file="main.tex")


def test_zero_occurrence_is_refused(_wire):
    with pytest.raises(ValueError, match="1-based"):
        rt.create_comment(_PID, "distribution shift", "x", occurrence=0)
