"""Regression tests for push verification (git_client._push_checked).

These pin the failure that made a write tool report success while the commit
never left the machine.

GitPython's ``Remote.push()`` does NOT raise when the remote rejects a ref.
It returns a ``PushInfoList`` carrying ERROR/REJECTED flags and stashes the
exception on ``.error``. The old code called ``origin.push()`` inside a
``try/except GitCommandError``, so a non-fast-forward rejection returned
normally, the tool answered "✅ Edited ...", and the commit sat unpushed in
the local clone. Every later read then served that stale local content back,
because the git-side readers read the clone rather than Overleaf.

Covered here, all offline against a bare repo on disk:

  - a rejected push raises instead of silently succeeding (the actual bug);
  - a non-fast-forward rejection is self-healed by rebasing onto the remote
    and retrying, so the ordinary "someone edited in the Overleaf UI" race
    resolves itself rather than stranding commits;
  - a rejection that is NOT a fast-forward problem propagates immediately
    instead of triggering a pointless rebase;
  - clones get pull.rebase set, so ``git pull`` can never abort with
    "Need to specify how to reconcile divergent branches".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from git import GitCommandError, Repo

from overleaf_mcp import git_client


def _commit(repo: Repo, name: str, text: str) -> None:
    Path(repo.working_tree_dir, name).write_text(text)
    repo.index.add([name])
    repo.index.commit(f"add {name}")


def _setup(tmp_path):
    """A bare 'Overleaf' remote plus a local clone with one shared commit."""
    remote = tmp_path / "remote.git"
    Repo.init(remote, bare=True, initial_branch="main")

    work = tmp_path / "work"
    repo = Repo.clone_from(str(remote), str(work))
    with repo.config_writer() as cw:
        cw.set_value("user", "name", "Test")
        cw.set_value("user", "email", "test@example.com")

    _commit(repo, "main.tex", "base\n")
    repo.git.push("--set-upstream", "origin", "HEAD:main")
    return remote, repo


class _FailingPush:
    """Stands in for Remote.push(): returns normally, carries an error.

    This is the shape that made the bug invisible — no exception, just an
    error parked on the returned list.
    """

    def __init__(self, message: str, flags: int = 0):
        self._message = message
        self._flags = flags

    def __call__(self, *a, **kw):
        err = GitCommandError("push", 1, b"", self._message.encode())
        flags = self._flags

        class _Info:
            pass

        info = _Info()
        info.flags = flags

        class _InfoList(list):
            def __init__(self):
                super().__init__([info])
                self.error = err

            def raise_if_error(self):
                raise err

        return _InfoList()


def test_rejected_push_is_never_reported_as_success(tmp_path):
    """The regression, end to end: a push that cannot land must raise.

    The remote moves ahead with a conflicting edit to the same file, so the
    rebase cannot save us either. Old behaviour: silent success.
    """
    remote, repo = _setup(tmp_path)

    other = Repo.clone_from(str(remote), str(tmp_path / "other"))
    with other.config_writer() as cw:
        cw.set_value("user", "name", "Overleaf")
        cw.set_value("user", "email", "ui@example.com")
    _commit(other, "main.tex", "edited in the Overleaf UI\n")
    other.git.push("origin", "HEAD:main")

    _commit(repo, "main.tex", "edited by the MCP\n")

    with pytest.raises((RuntimeError, GitCommandError, git_client.PushRejected)):
        git_client._push_checked(repo, "push(edit main.tex)")

    # And the clone must not be left parked mid-rebase.
    assert not (tmp_path / "work" / ".git" / "rebase-merge").exists()
    assert not (tmp_path / "work" / ".git" / "rebase-apply").exists()


def test_non_fast_forward_is_healed_by_rebase(tmp_path):
    """The Overleaf-UI race: remote moved ahead, our commit still lands."""
    remote, repo = _setup(tmp_path)

    # Someone edits in the Overleaf UI: a second clone pushes a new commit.
    other = Repo.clone_from(str(remote), str(tmp_path / "other"))
    with other.config_writer() as cw:
        cw.set_value("user", "name", "Overleaf")
        cw.set_value("user", "email", "ui@example.com")
    _commit(other, "from_ui.tex", "ui edit\n")
    other.git.push("origin", "HEAD:main")

    # Meanwhile we commit locally; a plain push here is non-fast-forward.
    _commit(repo, "from_mcp.tex", "mcp edit\n")

    git_client._push_checked(repo, "push(edit main.tex)")

    # Both commits are on the remote, and nothing was stranded locally.
    bare = Repo(str(remote))
    landed = {f for c in bare.iter_commits("main") for f in c.stats.files}
    assert "from_mcp.tex" in landed
    assert "from_ui.tex" in landed
    repo.git.fetch("origin")
    assert repo.git.rev_list("--count", "origin/main..HEAD").strip() == "0"


def test_unrelated_rejection_is_not_retried_as_rebase(tmp_path, monkeypatch):
    """A non-fast-forward heuristic must not swallow other push failures."""
    _, repo = _setup(tmp_path)

    calls: list[str] = []
    monkeypatch.setattr(
        type(repo.remotes.origin),
        "push",
        _FailingPush("fatal: Authentication failed", flags=0),
        raising=False,
    )
    monkeypatch.setattr(
        type(repo.git), "pull", lambda self, *a, **kw: calls.append("pull"), raising=False
    )

    with pytest.raises(GitCommandError):
        git_client._push_checked(repo, "push(test)")
    assert calls == [], "auth failure must not trigger a rebase attempt"


def test_rejection_flag_triggers_the_rebase_path(monkeypatch, tmp_path):
    """A REJECTED flag is what routes us to the rebase, not the stderr text.

    GitPython's porcelain stderr says only "failed to push some refs", with
    no mention of "non-fast-forward" or "rejected", so string-matching the
    message silently never fires.
    """
    _, repo = _setup(tmp_path)

    calls: list[str] = []
    monkeypatch.setattr(
        type(repo.remotes.origin),
        "push",
        _FailingPush(
            "error: failed to push some refs", flags=git_client.PushInfo.REJECTED
        ),
        raising=False,
    )
    monkeypatch.setattr(
        type(repo.git), "pull", lambda self, *a, **kw: calls.append("pull"), raising=False
    )

    with pytest.raises(RuntimeError):
        git_client._push_checked(repo, "push(test)")
    assert calls == ["pull"], "a rejected push must be rebased and retried once"


def test_clone_gets_a_reconcile_strategy(tmp_path):
    """pull.rebase must be set, or divergence bricks the clone permanently."""
    _, repo = _setup(tmp_path)
    git_client._config_git_user(repo)
    assert repo.config_reader().get_value("pull", "rebase") in (True, "true")
