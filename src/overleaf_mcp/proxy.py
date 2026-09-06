"""overleaf-mcp-proxy — run the server on another machine, upload from this one.

Use this as the MCP ``command`` when ``overleaf-mcp`` itself runs on a
different host over ssh::

    overleaf-mcp-proxy --host minipc -- 'set -a; . ~/.config/overleaf-mcp/env; set +a; exec ~/src/overleaf-mcp/.venv/bin/overleaf-mcp'

The proxy spawns ``ssh <host> <remote command>`` and pipes the MCP stdio
stream through unchanged, with one exception: a ``tools/call`` of
``upload_file`` whose ``source_path`` is a regular file on THIS machine.
For that call it streams the file into a fresh directory under the remote
inbox, rewrites ``source_path`` to that remote location, forwards the call,
and removes the directory once the server has answered.

The proxy holds no credentials and never talks to Overleaf.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import uuid
from pathlib import Path

UPLOAD_TOOL = "upload_file"
SOURCE_ARG = "source_path"
#: Relative to the remote ``$HOME`` (ssh commands start there).
DEFAULT_INBOX = ".cache/overleaf-mcp/inbox"


class TransferError(RuntimeError):
    """Copying a file to the remote host failed."""


# ── pure helpers ──────────────────────────────────────────────────────────

def local_upload_source(msg: object) -> Path | None:
    """Return the client-side file an ``upload_file`` call names, or ``None``.

    ``None`` means "forward unchanged": the message is not a ``tools/call``,
    is not ``upload_file``, has no ``source_path``, or names something that
    is not a regular file on this machine (for example a path that only
    exists on the host).
    """
    if not isinstance(msg, dict) or msg.get("method") != "tools/call":
        return None
    params = msg.get("params")
    if not isinstance(params, dict) or params.get("name") != UPLOAD_TOOL:
        return None
    args = params.get("arguments")
    if not isinstance(args, dict):
        return None
    src = args.get(SOURCE_ARG)
    if not isinstance(src, str) or not src:
        return None
    p = Path(src).expanduser()
    return p if p.is_file() else None


def rewrite_source(msg: dict, remote_path: str) -> dict:
    """Return a deep copy of ``msg`` with ``source_path`` replaced."""
    new = json.loads(json.dumps(msg))
    new["params"]["arguments"][SOURCE_ARG] = remote_path
    return new


def error_result(request_id: object, text: str) -> dict:
    """Build a tool result the model can see, in the server's own convention.

    ``server.handle_call_tool`` returns failures as a NORMAL result whose
    text starts with ``Error:`` (a raised exception would become a
    transport-level JSON-RPC error the model never sees). The proxy mirrors
    that so the client sees one shape for every failure.
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"content": [{"type": "text", "text": f"Error: {text}"}]},
    }


def remote_copy_command(remote_dir: str, remote_file: str) -> str:
    """Shell run on the host: make the dir, write stdin to the file, print its size."""
    d, f = shlex.quote(remote_dir), shlex.quote(remote_file)
    return f"mkdir -p {d} && cat > {f} && wc -c < {f}"


def server_visible_path(remote_file: str) -> str:
    """Path the SERVER should open: ``~/``-prefixed unless the inbox is absolute.

    ``git_client.upload_file`` calls ``Path(source_path).expanduser()``, so a
    ``~/`` prefix resolves under the server's own home regardless of its cwd.
    """
    return remote_file if remote_file.startswith("/") else "~/" + remote_file


# ── transfer ──────────────────────────────────────────────────────────────

class SshTransfer:
    """Copy client files to ``host`` over ssh, one connection per file."""

    def __init__(self, host: str, inbox: str = DEFAULT_INBOX, ssh_bin: str = "ssh"):
        self.host = host
        self.inbox = inbox.rstrip("/")
        self.ssh_bin = ssh_bin

    def put(self, local: Path) -> tuple[str, str]:
        """Stream ``local`` to the host and return ``(remote_dir, remote_file)``.

        Both are relative to the remote ``$HOME`` unless the inbox is
        absolute. Raises ``TransferError`` on any failure, including a
        byte-count mismatch after the write.
        """
        remote_dir = f"{self.inbox}/{uuid.uuid4().hex}"
        remote_file = f"{remote_dir}/{local.name}"
        expected = local.stat().st_size
        with local.open("rb") as fh:
            proc = subprocess.run(
                [self.ssh_bin, self.host, remote_copy_command(remote_dir, remote_file)],
                stdin=fh,
                capture_output=True,
                text=True,
            )
        if proc.returncode != 0:
            raise TransferError(
                f"ssh {self.host} exited {proc.returncode} while copying "
                f"'{local}': {proc.stderr.strip()}"
            )
        lines = proc.stdout.strip().splitlines()
        try:
            written = int(lines[-1].strip())
        except (IndexError, ValueError):
            self.remove(remote_dir)
            raise TransferError(
                f"could not read the byte count back from {self.host}: {proc.stdout!r}"
            )
        if written != expected:
            self.remove(remote_dir)
            raise TransferError(
                f"size mismatch copying '{local}': sent {expected} bytes, "
                f"host has {written}"
            )
        return remote_dir, remote_file

    def remove(self, remote_dir: str) -> None:
        """Best-effort delete of one inbox directory."""
        try:
            subprocess.run(
                [self.ssh_bin, self.host, f"rm -rf {shlex.quote(remote_dir)}"],
                # DEVNULL is load-bearing: ssh would otherwise read the MCP
                # client pipe (our stdin) and swallow messages.
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            pass


# ── stdio pump ────────────────────────────────────────────────────────────

class Proxy:
    """Pipe MCP stdio between the client and ``server``, intercepting uploads.

    ``server`` needs ``.stdin`` / ``.stdout`` binary streams and, for ``run``,
    ``.wait()``. ``transfer`` needs ``.put(Path)`` / ``.remove(str)``.
    """

    def __init__(self, server, transfer, out=None, cleanup_async: bool = True):
        self.server = server
        self.transfer = transfer
        self.out = out if out is not None else sys.stdout.buffer
        self.cleanup_async = cleanup_async
        self._out_lock = threading.Lock()
        self._pending: dict[str, str] = {}      # json.dumps(id) -> remote_dir
        self._pending_lock = threading.Lock()
        self._client_closed = False
        self._cleanup_threads: list[threading.Thread] = []

    # ── writes ──

    def _write_client(self, line: bytes) -> None:
        with self._out_lock:
            self.out.write(line if line.endswith(b"\n") else line + b"\n")
            self.out.flush()

    def _write_server(self, line: bytes) -> None:
        self.server.stdin.write(line if line.endswith(b"\n") else line + b"\n")
        self.server.stdin.flush()

    # ── client → server ──

    def handle_client_line(self, line: bytes) -> None:
        """Forward one client line, copying the file first if it is a local upload."""
        try:
            msg = json.loads(line)
        except ValueError:
            self._write_server(line)
            return
        local = local_upload_source(msg)
        if local is None:
            self._write_server(line)
            return
        try:
            remote_dir, remote_file = self.transfer.put(local)
        except (TransferError, OSError) as e:
            self._write_client(json.dumps(error_result(msg.get("id"), str(e))).encode())
            return
        with self._pending_lock:
            self._pending[json.dumps(msg.get("id"))] = remote_dir
        self._write_server(
            json.dumps(rewrite_source(msg, server_visible_path(remote_file))).encode()
        )

    # ── server → client ──

    def handle_server_line(self, line: bytes) -> None:
        """Forward one server line, then drop the inbox dir of an answered upload."""
        self._write_client(line)
        try:
            msg = json.loads(line)
        except ValueError:
            return
        if not isinstance(msg, dict) or "id" not in msg:
            return
        if "result" not in msg and "error" not in msg:
            return
        with self._pending_lock:
            remote_dir = self._pending.pop(json.dumps(msg["id"]), None)
        if remote_dir is None:
            return
        if self.cleanup_async:
            t = threading.Thread(target=self.transfer.remove, args=(remote_dir,), daemon=True)
            # Start INSIDE the lock. A drain's snapshot must never observe a
            # thread that has not been started yet, because Thread.join()
            # raises RuntimeError on one. Holding the lock across both means a
            # snapshot sees either a started thread or no thread at all.
            with self._pending_lock:
                self._cleanup_threads.append(t)
                t.start()
        else:
            self.transfer.remove(remote_dir)

    # ── lifecycle ──

    def _cleanup_pending(self) -> None:
        with self._pending_lock:
            dirs = list(self._pending.values())
            self._pending.clear()
        for d in dirs:
            self.transfer.remove(d)

    def _drain_cleanup(self, producer: threading.Thread) -> None:
        """Join every cleanup thread, including ones ``producer`` adds late.

        ``producer`` is the pump thread, the only code that appends to
        ``_cleanup_threads``. Stopping on an empty snapshot alone would race:
        the pump can append another thread just after that snapshot is taken,
        and it would never be joined. So an empty batch only ends the drain
        once the producer is finished. A producer that will not stop is given
        one bounded wait and then abandoned, rather than spun on forever.
        """
        while True:
            # Liveness is read BEFORE the snapshot on purpose. Reading it after
            # would let a producer append and then exit in between, so the
            # append is missed while is_alive() already reports False.
            alive = producer.is_alive()
            with self._pending_lock:
                batch = self._cleanup_threads[:]
                self._cleanup_threads.clear()
            for t in batch:
                t.join(timeout=30)
            if batch:
                continue
            if not alive:
                return
            producer.join(timeout=5)
            if producer.is_alive():
                return

    def _pump_server(self) -> None:
        for line in iter(self.server.stdout.readline, b""):
            self.handle_server_line(line)
        if not self._client_closed:
            # the server (or ssh) died under a live client: behave like ssh
            # would and end the session with its exit code
            self._cleanup_pending()
            rc = self.server.wait()
            with self._out_lock:
                self.out.flush()
            os._exit(rc if rc else 1)

    def run(self, inp=None) -> int:
        """Pump until the client closes stdin; return the server's exit code."""
        inp = inp if inp is not None else sys.stdin.buffer
        pump = threading.Thread(target=self._pump_server, daemon=True)
        pump.start()
        for line in iter(inp.readline, b""):
            self.handle_client_line(line)
        self._client_closed = True
        try:
            self.server.stdin.close()
        except OSError:
            pass
        rc = self.server.wait()
        pump.join(timeout=5)
        self._cleanup_pending()
        self._drain_cleanup(pump)
        return rc


# ── CLI ───────────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(
        prog="overleaf-mcp-proxy",
        description=(
            "Run overleaf-mcp on another host over ssh while letting upload_file "
            "read files from this machine."
        ),
    )
    ap.add_argument(
        "--host", required=True,
        help="ssh destination running overleaf-mcp (an alias from ~/.ssh/config works)",
    )
    ap.add_argument(
        "--inbox", default=DEFAULT_INBOX,
        help="remote directory for in-flight uploads; relative to the remote $HOME "
             "unless absolute (default: %(default)s)",
    )
    ap.add_argument("--ssh-bin", default="ssh", help=argparse.SUPPRESS)
    ap.add_argument(
        "remote_command", nargs=argparse.REMAINDER,
        help="command that starts overleaf-mcp on the host; put it after --",
    )
    ns = ap.parse_args(argv)

    words = list(ns.remote_command)
    if words and words[0] == "--":
        words = words[1:]
    if not words:
        ap.error("remote command required after --")
    remote_cmd = " ".join(words)

    server = subprocess.Popen(
        [ns.ssh_bin, ns.host, remote_cmd],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    )
    rc = Proxy(server, SshTransfer(ns.host, ns.inbox, ns.ssh_bin)).run()
    sys.exit(rc)


if __name__ == "__main__":
    main()
