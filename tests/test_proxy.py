"""tests/test_proxy.py — overleaf-mcp-proxy: upload a client-side file to a server on another host.

The proxy sits in the MCP stdio path, so a bug here breaks EVERY tool, not
just upload_file. The tests therefore cover three layers separately:

  * pure helpers (message classification / rewriting / error shape),
  * the Proxy pump with a fake transfer (no ssh, no subprocess),
  * one real subprocess run with a fake ``ssh`` binary that executes the
    "remote" command locally, proving bytes arrive intact and the inbox is
    cleaned up.
"""

import io
import json
import os
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'src'))

from overleaf_mcp import proxy  # noqa: E402


def _upload_call(source_path, request_id=7):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": "upload_file",
            "arguments": {
                "project_id": "abc123",
                "file_path": "figures/fig.png",
                "source_path": source_path,
                "overwrite": True,
            },
        },
    }


# ── local_upload_source ──────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    ["not", "a", "dict"],
    {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
     "params": {"name": "read_file", "arguments": {"source_path": "/etc/hosts"}}},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
     "params": {"name": "upload_file"}},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
     "params": {"name": "upload_file", "arguments": {"file_path": "a.png"}}},
    {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
     "params": {"name": "upload_file", "arguments": {"source_path": ""}}},
])
def test_local_upload_source_ignores_non_upload_messages(msg):
    assert proxy.local_upload_source(msg) is None


def test_local_upload_source_returns_path_for_local_file(tmp_path):
    f = tmp_path / "fig.png"
    f.write_bytes(b"\x89PNG")
    assert proxy.local_upload_source(_upload_call(str(f))) == f


def test_local_upload_source_none_when_path_absent_or_directory(tmp_path):
    assert proxy.local_upload_source(_upload_call(str(tmp_path / "missing.png"))) is None
    assert proxy.local_upload_source(_upload_call(str(tmp_path))) is None


# ── rewrite_source / error_result ─────────────────────────────────────────

def test_rewrite_source_replaces_only_source_path():
    original = _upload_call("/mac/fig.png")
    new = proxy.rewrite_source(original, "~/.cache/overleaf-mcp/inbox/x/fig.png")
    assert new["params"]["arguments"]["source_path"] == "~/.cache/overleaf-mcp/inbox/x/fig.png"
    assert new["params"]["arguments"]["file_path"] == "figures/fig.png"
    assert new["params"]["arguments"]["overwrite"] is True
    assert new["id"] == 7
    # the caller's dict is untouched
    assert original["params"]["arguments"]["source_path"] == "/mac/fig.png"


def test_error_result_matches_server_convention():
    r = proxy.error_result(42, "ssh minipc exited 255")
    assert r == {
        "jsonrpc": "2.0",
        "id": 42,
        "result": {"content": [{"type": "text", "text": "Error: ssh minipc exited 255"}]},
    }
    # the server never sets isError (see server.handle_call_tool); neither do we
    assert "isError" not in r["result"]


# ── shell snippets ────────────────────────────────────────────────────────

def test_remote_copy_command_quotes_paths():
    cmd = proxy.remote_copy_command(".cache/in box/abc", ".cache/in box/abc/my fig.png")
    assert cmd == (
        "mkdir -p '.cache/in box/abc' && cat > '.cache/in box/abc/my fig.png' "
        "&& wc -c < '.cache/in box/abc/my fig.png'"
    )


def test_server_visible_path_prefixes_home_for_relative_inbox():
    assert proxy.server_visible_path(".cache/overleaf-mcp/inbox/x/fig.png") == "~/.cache/overleaf-mcp/inbox/x/fig.png"
    assert proxy.server_visible_path("/srv/inbox/x/fig.png") == "/srv/inbox/x/fig.png"


# ── SshTransfer (against a fake ssh that runs the command locally) ────────

FAKE_SSH = """#!/bin/sh
# fake ssh for tests: ignore the host, run the remote command in the cwd
shift
exec sh -c "$*"
"""


@pytest.fixture
def fake_ssh(tmp_path):
    p = tmp_path / "ssh"
    p.write_text(FAKE_SSH)
    p.chmod(0o755)
    return str(p)


def test_put_streams_bytes_and_returns_paths(tmp_path, fake_ssh, monkeypatch):
    monkeypatch.chdir(tmp_path)          # relative inbox lands under tmp_path
    src = tmp_path / "fig.png"
    payload = bytes(range(256)) * 300    # 76.8 KB, every byte value
    src.write_bytes(payload)

    t = proxy.SshTransfer("ignored-host", inbox="inbox", ssh_bin=fake_ssh)
    remote_dir, remote_file = t.put(src)

    assert remote_dir.startswith("inbox/")
    assert remote_file == f"{remote_dir}/fig.png"
    assert (tmp_path / remote_file).read_bytes() == payload

    t.remove(remote_dir)
    assert not (tmp_path / remote_dir).exists()


def test_put_raises_when_ssh_fails(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    bad = tmp_path / "ssh"
    bad.write_text("#!/bin/sh\necho 'Permission denied' >&2\nexit 255\n")
    bad.chmod(0o755)
    src = tmp_path / "fig.png"
    src.write_bytes(b"x")

    t = proxy.SshTransfer("h", inbox="inbox", ssh_bin=str(bad))
    with pytest.raises(proxy.TransferError, match="exited 255.*Permission denied"):
        t.put(src)


def test_put_raises_on_size_mismatch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    # a "remote" that swallows stdin and reports the wrong size
    lying = tmp_path / "ssh"
    lying.write_text("#!/bin/sh\ncat >/dev/null\necho 1\n")
    lying.chmod(0o755)
    src = tmp_path / "fig.png"
    src.write_bytes(b"abcdef")

    t = proxy.SshTransfer("h", inbox="inbox", ssh_bin=str(lying))
    with pytest.raises(proxy.TransferError, match="sent 6 bytes, host has 1"):
        t.put(src)


def test_put_parses_last_stdout_line(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    chatty = tmp_path / "ssh"
    # a remote .bashrc that prints a banner before the real command runs
    chatty.write_text('#!/bin/sh\necho "Welcome to minipc"\nshift\nexec sh -c "$*"\n')
    chatty.chmod(0o755)
    src = tmp_path / "fig.png"
    src.write_bytes(b"abcdef")

    t = proxy.SshTransfer("h", inbox="inbox", ssh_bin=str(chatty))
    _, remote_file = t.put(src)
    assert (tmp_path / remote_file).read_bytes() == b"abcdef"


# ── Proxy pump with a fake transfer ───────────────────────────────────────

class FakeTransfer:
    def __init__(self, fail=None, remove_delay=0.0):
        self.fail = fail
        self.remove_delay = remove_delay
        self.put_calls = []
        self.removed = []
        self._removed_lock = threading.Lock()

    def put(self, local):
        if self.fail:
            raise self.fail
        self.put_calls.append(local)
        return "inbox/deadbeef", f"inbox/deadbeef/{local.name}"

    def remove(self, remote_dir):
        if self.remove_delay:
            time.sleep(self.remove_delay)
        with self._removed_lock:
            self.removed.append(remote_dir)


class FakeServer:
    """Just the two pipes Proxy touches."""
    def __init__(self):
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()


def _make_proxy(transfer):
    server = FakeServer()
    out = io.BytesIO()
    p = proxy.Proxy(server, transfer, out=out, cleanup_async=False)
    return p, server, out


def _line(obj):
    return (json.dumps(obj) + "\n").encode()


def test_non_upload_lines_pass_through_verbatim():
    p, server, out = _make_proxy(FakeTransfer())
    raw = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}\n'
    p.handle_client_line(raw)
    p.handle_client_line(b"not json at all\n")
    assert server.stdin.getvalue() == raw + b"not json at all\n"
    assert out.getvalue() == b""


def test_remote_only_path_is_forwarded_unchanged(tmp_path):
    p, server, out = _make_proxy(FakeTransfer())
    raw = _line(_upload_call(str(tmp_path / "only-on-minipc.png")))
    p.handle_client_line(raw)
    assert server.stdin.getvalue() == raw


def test_local_upload_is_copied_rewritten_and_tracked(tmp_path):
    f = tmp_path / "fig.png"
    f.write_bytes(b"\x89PNG")
    t = FakeTransfer()
    p, server, out = _make_proxy(t)

    p.handle_client_line(_line(_upload_call(str(f), request_id=9)))

    assert t.put_calls == [f]
    sent = json.loads(server.stdin.getvalue())
    assert sent["params"]["arguments"]["source_path"] == "~/inbox/deadbeef/fig.png"
    assert sent["params"]["arguments"]["file_path"] == "figures/fig.png"
    assert sent["id"] == 9
    assert out.getvalue() == b""          # nothing answered yet


def test_transfer_failure_is_answered_to_client_not_forwarded(tmp_path):
    f = tmp_path / "fig.png"
    f.write_bytes(b"\x89PNG")
    t = FakeTransfer(fail=proxy.TransferError("ssh minipc exited 255"))
    p, server, out = _make_proxy(t)

    p.handle_client_line(_line(_upload_call(str(f), request_id=9)))

    assert server.stdin.getvalue() == b""
    reply = json.loads(out.getvalue())
    assert reply["id"] == 9
    assert reply["result"]["content"][0]["text"] == "Error: ssh minipc exited 255"


def test_response_is_forwarded_and_inbox_removed(tmp_path):
    f = tmp_path / "fig.png"
    f.write_bytes(b"\x89PNG")
    t = FakeTransfer()
    p, server, out = _make_proxy(t)
    p.handle_client_line(_line(_upload_call(str(f), request_id=9)))

    unrelated = _line({"jsonrpc": "2.0", "id": 8, "result": {"content": []}})
    p.handle_server_line(unrelated)
    assert t.removed == []

    notification = _line({"jsonrpc": "2.0", "method": "notifications/progress", "params": {}})
    p.handle_server_line(notification)
    assert t.removed == []

    answer = _line({"jsonrpc": "2.0", "id": 9, "result": {"content": [{"type": "text", "text": "ok"}]}})
    p.handle_server_line(answer)
    assert t.removed == ["inbox/deadbeef"]
    assert out.getvalue() == unrelated + notification + answer

    # a second response with the same id must not remove twice
    p.handle_server_line(answer)
    assert t.removed == ["inbox/deadbeef"]


class _ClosingStdin(io.BytesIO):
    """BytesIO whose close() also ends the fake server's stdout, like a child exiting."""
    def __init__(self, on_close):
        super().__init__()
        self._on_close = on_close

    def close(self):
        self._on_close()
        super().close()


class PipeServer:
    """stdout stays open until stdin is closed, like a real child process."""
    def __init__(self):
        r, w = os.pipe()
        self.stdout = os.fdopen(r, "rb")
        self._stdout_w = w
        self.stdin = _ClosingStdin(self._end_stdout)
        self.returncode = None

    def _end_stdout(self):
        if self._stdout_w is not None:
            os.close(self._stdout_w)
            self._stdout_w = None

    def wait(self, timeout=None):
        self.returncode = 0
        return 0

    def poll(self):
        return self.returncode


def test_run_closes_server_stdin_and_cleans_leftovers(tmp_path):
    """Client hangs up after sending an upload the server never answered."""
    f = tmp_path / "fig.png"
    f.write_bytes(b"\x89PNG")
    t = FakeTransfer()
    server = PipeServer()
    out = io.BytesIO()
    p = proxy.Proxy(server, t, out=out, cleanup_async=False)
    inp = io.BytesIO(_line(_upload_call(str(f), request_id=3)))

    rc = p.run(inp)

    assert rc == 0
    assert server.stdin.closed
    assert t.removed == ["inbox/deadbeef"]
    server.stdout.close()


def test_run_joins_async_cleanup_thread_before_returning(tmp_path):
    """cleanup_async=True: run() must wait for the background remove() thread."""
    f = tmp_path / "fig.png"
    f.write_bytes(b"\x89PNG")
    t = FakeTransfer(remove_delay=0.05)
    server = PipeServer()
    out = io.BytesIO()
    p = proxy.Proxy(server, t, out=out, cleanup_async=True)

    p.handle_client_line(_line(_upload_call(str(f), request_id=5)))
    answer = _line({"jsonrpc": "2.0", "id": 5, "result": {"content": [{"type": "text", "text": "ok"}]}})
    p.handle_server_line(answer)

    rc = p.run(io.BytesIO(b""))

    assert rc == 0
    assert t.removed == ["inbox/deadbeef"]
    server.stdout.close()


class _LateAppendingProducer:
    """Duck-typed stand-in for the pump thread: only ``is_alive``/``join``.

    On its FIRST ``is_alive()`` call only, it appends a real cleanup thread to
    ``proxy_obj._cleanup_threads`` (under ``_pending_lock``) and starts it,
    then returns ``False``; later calls return ``False`` and do nothing.
    ``join()`` is a no-op. This makes the producer-appends-late race in
    ``_drain_cleanup`` reproducible with no extra threads of its own: the only
    thing that decides the outcome is whether liveness is read before or
    after the snapshot. The transfer's ``remove`` is given a short delay so a
    reverted ordering (which returns without joining) reliably misses it,
    instead of racing a near-instant append against the assertion below.
    """

    def __init__(self, proxy_obj, transfer, remote_dir):
        self._proxy_obj = proxy_obj
        self._transfer = transfer
        self._remote_dir = remote_dir
        self._first_call = True

    def is_alive(self):
        if self._first_call:
            self._first_call = False
            t = threading.Thread(target=self._transfer.remove, args=(self._remote_dir,))
            with self._proxy_obj._pending_lock:
                self._proxy_obj._cleanup_threads.append(t)
                t.start()
        return False

    def join(self, timeout=None):
        pass


def test_drain_cleanup_reads_liveness_before_snapshot():
    """Liveness must be read before the snapshot: reading it after would let
    the producer's append land in the gap and be missed by an empty snapshot,
    so the drain would return without ever joining the late thread."""
    t = FakeTransfer(remove_delay=0.05)
    proxy_obj = proxy.Proxy(FakeServer(), t, out=io.BytesIO(), cleanup_async=True)
    producer = _LateAppendingProducer(proxy_obj, t, "late-dir")

    proxy_obj._drain_cleanup(producer)

    assert t.removed == ["late-dir"]


# ── main(): argument handling and a real subprocess round trip ────────────

FAKE_SERVER = r'''
import json, os, sys
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue                       # notification: nothing to answer
    args = m.get("params", {}).get("arguments", {})
    src = args.get("source_path")
    size = os.path.getsize(src) if src and os.path.exists(src) else -1
    reply = {"jsonrpc": "2.0", "id": m["id"],
             "result": {"content": [{"type": "text", "text": f"got {src} size {size}"}]}}
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
'''


def test_main_requires_remote_command(capsys):
    with pytest.raises(SystemExit) as e:
        proxy.main(["--host", "h"])
    assert e.value.code == 2
    assert "remote command" in capsys.readouterr().err


def test_end_to_end_with_fake_ssh(tmp_path, fake_ssh):
    fake_server = tmp_path / "fake_server.py"
    fake_server.write_text(FAKE_SERVER)
    inbox = tmp_path / "inbox"          # absolute, so the server sees it as-is
    fig = tmp_path / "fig.png"
    payload = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 100
    fig.write_bytes(payload)

    src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    proc = subprocess.Popen(
        [sys.executable, "-m", "overleaf_mcp.proxy",
         "--host", "ignored", "--inbox", str(inbox), "--ssh-bin", fake_ssh,
         "--", sys.executable, str(fake_server)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=tmp_path, env=env,
    )
    try:
        proc.stdin.write(_line({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        proc.stdin.write(_line({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        proc.stdin.write(_line(_upload_call(str(fig), request_id=2)))
        proc.stdin.flush()

        first = json.loads(proc.stdout.readline())
        second = json.loads(proc.stdout.readline())
    finally:
        proc.stdin.close()
        try:
            rc = proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()

    assert rc == 0
    assert first["id"] == 1
    assert first["result"]["content"][0]["text"] == "got None size -1"

    assert second["id"] == 2
    text = second["result"]["content"][0]["text"]
    assert text.startswith(f"got {inbox}/")
    assert text.endswith(f"/fig.png size {len(payload)}")

    # the per-call inbox directory was removed after the response
    assert not inbox.exists() or list(inbox.iterdir()) == []


FAKE_SERVER_DIES = r'''
import json, sys
line = sys.stdin.readline()
m = json.loads(line)
reply = {"jsonrpc": "2.0", "id": m["id"],
         "result": {"content": [{"type": "text", "text": "ok"}]}}
sys.stdout.write(json.dumps(reply) + "\n")
sys.stdout.flush()
'''


def test_proxy_exits_when_server_dies_under_live_client(tmp_path, fake_ssh):
    """_pump_server's server-death branch: the server's stdout hits EOF while
    the client (this test) keeps its stdin open, so the proxy must notice and
    exit on its own rather than hang forever waiting on the client."""
    fake_server = tmp_path / "dying_server.py"
    fake_server.write_text(FAKE_SERVER_DIES)

    src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    proc = subprocess.Popen(
        [sys.executable, "-m", "overleaf_mcp.proxy",
         "--host", "ignored", "--ssh-bin", fake_ssh,
         "--", sys.executable, str(fake_server)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=tmp_path, env=env,
    )
    try:
        proc.stdin.write(_line({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}))
        proc.stdin.flush()
        reply = json.loads(proc.stdout.readline())
        assert reply["id"] == 1

        # client stdin is left OPEN here: the proxy must still exit because
        # the server died, not because the client hung up
        rc = proc.wait(timeout=10)
        assert rc == 1
    finally:
        proc.kill()
        proc.wait()


# ── contract with server.py ───────────────────────────────────────────────

def test_upload_tool_contract_is_pinned():
    """Renaming upload_file or source_path in server.py must break this test,
    because the proxy matches both by name."""
    from overleaf_mcp import server as srv
    by_name = {t.name: t for t in srv._TOOLS}
    assert proxy.UPLOAD_TOOL in by_name
    props = by_name[proxy.UPLOAD_TOOL].input_schema["properties"]
    assert proxy.SOURCE_ARG in props
    assert proxy.SOURCE_ARG in by_name[proxy.UPLOAD_TOOL].input_schema["required"]
