"""Tests for conflict-free writes: re-anchored edits and guarded rewrites.

``edit_file`` is a search-and-replace, so it never needs a three-way merge.
If Overleaf gains a commit mid-write the push is rejected; rather than
rebasing a commit onto text it was not written against, the write throws its
commit away, takes the remote's content, and runs the same replacement over
it. Either the anchor survives (the edit lands) or it does not (a clear
error), so text conflicts stop being a category.

``rewrite_file`` cannot be re-derived that way — the caller composed the whole
file against one specific version — so it gets optimistic concurrency instead
via ``expected_sha``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git import Repo

from overleaf_mcp import git_client


class _Project:
    """Minimal stand-in for ProjectConfig."""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.git_token = "unused"


def _setup(tmp_path, monkeypatch):
    """A bare 'Overleaf' remote, plus the MCP clone wired to it."""
    remote = tmp_path / "remote.git"
    Repo.init(remote, bare=True, initial_branch="main")

    seed = Repo.clone_from(str(remote), str(tmp_path / "seed"))
    with seed.config_writer() as cw:
        cw.set_value("user", "name", "Seed")
        cw.set_value("user", "email", "seed@example.com")
    Path(seed.working_tree_dir, "main.tex").write_text("alpha\nbeta\ngamma\n")
    seed.index.add(["main.tex"])
    seed.index.commit("seed")
    seed.git.push("--set-upstream", "origin", "HEAD:main")

    cache = tmp_path / "cache"
    cache.mkdir()
    monkeypatch.setattr(git_client, "TEMP_DIR", str(cache))
    monkeypatch.setattr(git_client, "_git_url", lambda project: str(remote))
    monkeypatch.setattr(git_client, "_project_tag", lambda project: "(test)")
    monkeypatch.setattr(git_client, "write_metadata", lambda *a, **kw: None)
    monkeypatch.setattr(git_client, "_PULL_TTL", 0)

    return remote, seed, _Project("proj")


def _remote_edit(seed: Repo, text: str, name: str = "main.tex") -> None:
    """Simulate somebody saving in the Overleaf UI."""
    seed.git.pull("--rebase", "origin", "main")
    Path(seed.working_tree_dir, name).write_text(text)
    seed.index.add([name])
    seed.index.commit(f"ui edit {name}")
    seed.git.push("origin", "HEAD:main")


def _remote_content(remote: Path, tmp_path: Path, name: str = "main.tex") -> str:
    check = Repo.clone_from(str(remote), str(tmp_path / f"check-{name}-{id(name)}"))
    return Path(check.working_tree_dir, name).read_text()


def test_edit_reapplies_against_a_moved_remote(tmp_path, monkeypatch):
    """The race that used to strand commits: both edits must survive."""
    remote, seed, project = _setup(tmp_path, monkeypatch)

    # Prime the clone so the MCP side is one commit behind once the UI saves.
    git_client.read_file(project, "main.tex")
    _remote_edit(seed, "alpha\nbeta\ngamma\ndelta-from-ui\n")

    git_client.edit_file(project, "main.tex", "beta", "BETA-from-mcp")

    final = _remote_content(remote, tmp_path)
    assert "BETA-from-mcp" in final, "our edit must land"
    assert "delta-from-ui" in final, "the UI edit must not be clobbered"


def test_edit_reports_a_vanished_anchor_rather_than_conflicting(tmp_path, monkeypatch):
    """If the remote deleted our anchor, say so; do not raise a merge conflict.

    Writes force a pull first, so this is caught before anything is written
    and the preview shows the caller the *current* text. The message stays
    the plain "not found" here on purpose: at this point we cannot tell a
    document that moved from an anchor that was simply mistyped.
    """
    remote, seed, project = _setup(tmp_path, monkeypatch)

    git_client.read_file(project, "main.tex")
    _remote_edit(seed, "alpha\nGAMMA-ONLY\n")  # 'beta' is gone upstream

    with pytest.raises(ValueError) as excinfo:
        git_client.edit_file(project, "main.tex", "beta", "BETA-from-mcp")

    message = str(excinfo.value)
    assert "old_string not found" in message
    assert "GAMMA-ONLY" in message, "preview must show the current remote text"
    assert "GAMMA-ONLY" in _remote_content(remote, tmp_path), "remote untouched"


def test_a_midflight_change_explains_itself(tmp_path, monkeypatch):
    """When we *know* the document moved under us, the error has to say so.

    Drives the retry path: the first push is rejected after the remote has
    already dropped our anchor, so the re-derivation fails and the caller is
    told the text changed rather than being handed a bare "not found".
    """
    remote, seed, project = _setup(tmp_path, monkeypatch)
    git_client.read_file(project, "main.tex")

    real_push = git_client._push_once
    calls = {"n": 0}

    def flaky_push(repo):
        calls["n"] += 1
        if calls["n"] == 1:
            _remote_edit(seed, "alpha\nGAMMA-ONLY\n")  # moves while we push
            raise git_client.PushRejected("simulated non-fast-forward")
        return real_push(repo)

    monkeypatch.setattr(git_client, "_push_once", flaky_push)

    with pytest.raises(ValueError) as excinfo:
        git_client.edit_file(project, "main.tex", "beta", "BETA-from-mcp")

    assert "changed on Overleaf" in str(excinfo.value)
    assert "GAMMA-ONLY" in _remote_content(remote, tmp_path), "remote untouched"


def test_rewrite_refuses_when_the_file_moved(tmp_path, monkeypatch):
    """Optimistic concurrency: a stale whole-file rewrite is refused."""
    remote, seed, project = _setup(tmp_path, monkeypatch)

    original = git_client.read_file(project, "main.tex")
    stale_sha = git_client.content_sha(original)

    _remote_edit(seed, "alpha\nbeta\ngamma\nimportant-ui-work\n")

    with pytest.raises(git_client.ContentConflict) as excinfo:
        git_client.rewrite_file(
            project, "main.tex", "wholesale replacement\n", expected_sha=stale_sha
        )

    assert "has changed since you read it" in str(excinfo.value)
    assert "important-ui-work" in _remote_content(remote, tmp_path), "not clobbered"


def test_rewrite_succeeds_when_the_sha_still_matches(tmp_path, monkeypatch):
    """The guard must not block the ordinary uncontended case."""
    remote, seed, project = _setup(tmp_path, monkeypatch)

    current = git_client.read_file(project, "main.tex")
    out = git_client.rewrite_file(
        project, "main.tex", "replaced\n", expected_sha=git_client.content_sha(current)
    )

    assert "replaced" in _remote_content(remote, tmp_path)
    assert git_client.content_sha("replaced\n") in out, "returns the new sha"


def test_rewrite_without_a_sha_still_works(tmp_path, monkeypatch):
    """expected_sha is opt-in; omitting it keeps the old behaviour."""
    remote, seed, project = _setup(tmp_path, monkeypatch)
    git_client.rewrite_file(project, "main.tex", "no guard\n")
    assert "no guard" in _remote_content(remote, tmp_path)
