"""Create anchored comments over Overleaf's realtime channel.

Everything else in this package is REST. This module is the exception, and
it exists because *creating* a comment cannot be done over REST at all.
Overleaf's own review panel does it in two halves
(``addComment`` in ``review-panel/context/threads-context.tsx``):

    POST /project/<pid>/thread/<tid>/messages   {"content": …}   ← REST
    currentDocument.submitOp({c: text, p: pos, t: tid})          ← realtime

The thread id is minted CLIENT-side, the message is posted to it over REST,
and only then does a comment operation bind that id to a character range in
the document. Skip the second half and you get an orphan thread that appears
in ``/threads`` but is anchored to nothing and invisible in the sidebar.

Three undocumented things are needed to get onto the channel at all, none of
which are in the open-source repo, all three found by probing production:

1. The handshake must carry ``?projectId=<pid>``. Without it the server
   accepts the socket and then immediately sends ``connectionRejected:
   "missing/bad ?projectId=... query flag on handshake"``.
2. The ``GCLB`` load-balancer affinity cookie set by the handshake response
   must be replayed on the websocket upgrade. Without it the upgrade lands on
   a different backend, which has never seen the session id, and socket.io
   answers ``7:::1+0`` — "client not handshaken".
3. The protocol is socket.io **0.9** framing (``5:<id>+::{…}`` to emit,
   ``6:::<id>+[…]`` for the ack), not the modern Engine.IO wire format.

This is materially more fragile than the REST endpoints: it depends on the
socket.io 0.9 framing, on the ShareJS op shape, and on ``ol-otMigrationStage``
still being 0. Guard accordingly and prefer REST wherever REST suffices.
"""

from __future__ import annotations

import binascii
import json
import logging
import os
import time

import httpx

from .config import OVERLEAF_BASE_URL
from .credentials import get_session

logger = logging.getLogger("overleaf-mcp")

_HAS_WS = False
try:
    import websocket  # type: ignore[import-untyped]

    _HAS_WS = True
except ImportError:
    pass

_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)


def new_thread_id() -> str:
    """Mint a client-side thread id, as ``RangesTracker.generateId()`` does.

    Overleaf shapes these like a Mongo ObjectId: 4 big-endian timestamp bytes
    followed by 8 random ones, hex-encoded to 24 characters.
    """
    return format(int(time.time()), "08x") + binascii.hexlify(os.urandom(8)).decode()


class RealtimeError(RuntimeError):
    """The realtime channel refused us, or the document op was not applied."""


class _Realtime:
    """One socket.io 0.9 session against a single project."""

    def __init__(self, project_id: str) -> None:
        if not _HAS_WS:
            raise ImportError(
                "websocket-client required for comment creation: "
                "pip install overleaf-mcp[compile]"
            )
        self.project_id = project_id
        self._ws = None
        self._msg_id = 0
        self._client = httpx.Client(
            headers={
                "Cookie": f"overleaf_session2={get_session()}",
                "User-Agent": _UA,
            },
            follow_redirects=True,
            timeout=30,
        )

    # -- lifecycle ------------------------------------------------------
    def __enter__(self) -> "_Realtime":
        pid = self.project_id
        # Warm the session on the project page first: this is what puts the
        # GCLB affinity cookie in the jar before the handshake.
        self._client.get(f"{OVERLEAF_BASE_URL}/project/{pid}")
        r = self._client.get(
            f"{OVERLEAF_BASE_URL}/socket.io/1/"
            f"?projectId={pid}&t={int(time.time() * 1000)}",
            headers={"Referer": f"{OVERLEAF_BASE_URL}/project/{pid}"},
        )
        r.raise_for_status()
        sid = r.text.split(":")[0]
        cookies = "; ".join(f"{c.name}={c.value}" for c in self._client.cookies.jar)
        ws_url = (
            f"{OVERLEAF_BASE_URL.replace('https://', 'wss://')}"
            f"/socket.io/1/websocket/{sid}?projectId={pid}"
        )
        self._ws = websocket.create_connection(
            ws_url,
            header=[f"Cookie: {cookies}", f"User-Agent: {_UA}"],
            origin=OVERLEAF_BASE_URL,
            timeout=30,
        )
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._client.close()

    # -- socket.io 0.9 framing -------------------------------------------
    def _emit(self, name: str, args: list) -> int:
        self._msg_id += 1
        self._ws.send(
            f"5:{self._msg_id}+::" + json.dumps({"name": name, "args": args})
        )
        return self._msg_id

    def _recv_until(self, predicate, limit: int = 20):
        for _ in range(limit):
            try:
                frame = self._ws.recv()
            except Exception as e:
                raise RealtimeError(f"realtime connection dropped: {e}") from e
            if '"connectionRejected"' in frame:
                raise RealtimeError(f"Overleaf rejected the connection: {frame[:200]}")
            hit = predicate(frame)
            if hit:
                return frame
        raise RealtimeError("timed out waiting for the expected realtime frame")

    # -- operations -------------------------------------------------------
    def join_project(self) -> dict:
        """Join the project and return its doc-id → path map."""
        self._emit("joinProject", [{"project_id": self.project_id}])
        frame = self._recv_until(lambda f: '"joinProjectResponse"' in f)
        args = json.loads(frame.split(":::", 1)[1])["args"]
        project = args[0]["project"]
        docs: dict[str, str] = {}

        def walk(folder: dict, prefix: str = "") -> None:
            for d in folder.get("docs") or []:
                docs[d["_id"]] = prefix + d["name"]
            for sub in folder.get("folders") or []:
                walk(sub, prefix + sub["name"] + "/")

        for root in project.get("rootFolder") or []:
            walk(root)
        self.root_doc_id = project.get("rootDoc_id")
        return docs

    def join_doc(self, doc_id: str) -> tuple[str, int]:
        """Join a document, returning its full text and current version."""
        mid = self._emit("joinDoc", [doc_id, -1, {"encodeRanges": True}])
        frame = self._recv_until(lambda f: f.startswith(f"6:::{mid}"))
        ack = json.loads(frame.split("+", 1)[1])
        lines, version = ack[1], ack[2]
        return "\n".join(lines), version

    def submit_comment_op(
        self, doc_id: str, anchor_text: str, position: int, thread_id: str, version: int
    ) -> None:
        """Bind ``thread_id`` to a character range, then confirm it applied."""
        op = {
            "doc": doc_id,
            "op": [{"c": anchor_text, "p": position, "t": thread_id}],
            "v": version,
        }
        self._emit("applyOtUpdate", [doc_id, op])
        self._recv_until(lambda f: '"otUpdateApplied"' in f)


def create_comment(
    project_id: str,
    anchor_text: str,
    content: str,
    file: str | None = None,
    occurrence: int = 1,
) -> dict:
    """Create a real, anchored comment thread on a passage of the document.

    ``anchor_text`` must appear verbatim in the file; ``occurrence`` picks
    which match when it appears more than once (1-based). The thread id is
    minted locally, the message posted over REST, and the anchor bound over
    the realtime channel — all three are required for the comment to show up
    in a co-author's review panel.
    """
    if not content or not content.strip():
        raise ValueError("Comment content cannot be empty.")
    if not anchor_text:
        raise ValueError("anchor_text is required — a comment must anchor to a passage.")
    if occurrence < 1:
        raise ValueError("occurrence is 1-based.")

    # Imported here rather than at module scope: threads imports compile,
    # which is optional, and this module must stay importable without it.
    from .threads import _csrf

    with _Realtime(project_id) as rt:
        docs = rt.join_project()
        if file:
            matches = [d for d, path in docs.items() if path == file]
            if not matches:
                raise ValueError(
                    f"No document named {file!r} in this project. "
                    f"Available: {sorted(docs.values())}"
                )
            doc_id = matches[0]
        else:
            doc_id = rt.root_doc_id
            if not doc_id:
                raise RealtimeError("Project has no root document; pass `file`.")

        text, version = rt.join_doc(doc_id)

        pos = -1
        for _ in range(occurrence):
            pos = text.find(anchor_text, pos + 1)
            if pos < 0:
                break
        if pos < 0:
            raise ValueError(
                f"Could not find occurrence {occurrence} of {anchor_text!r} in "
                f"{docs.get(doc_id, doc_id)}. The anchor must match the source "
                "exactly, including LaTeX markup."
            )

        thread_id = new_thread_id()
        r = httpx.post(
            f"{OVERLEAF_BASE_URL}/project/{project_id}/thread/{thread_id}/messages",
            json={"content": content},
            headers={
                "Cookie": f"overleaf_session2={get_session()}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "x-csrf-token": _csrf(project_id),
                "User-Agent": _UA,
            },
            timeout=30,
        )
        r.raise_for_status()

        rt.submit_comment_op(doc_id, anchor_text, pos, thread_id, version)

    logger.info("created comment thread %s on %s", thread_id, doc_id)
    return {
        "thread_id": thread_id,
        "doc_id": doc_id,
        "file": docs.get(doc_id),
        "quoted_text": anchor_text,
        "position": pos,
        "content": content,
    }
