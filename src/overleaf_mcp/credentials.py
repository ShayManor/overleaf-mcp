"""Credential lookup for Overleaf MCP.

Credentials are supplied via environment variables by the host that
launches the server (e.g. an MCP client's config, a shell profile, or a
docker-compose file):

* ``OVERLEAF_SESSION``    — value of the ``overleaf_session2`` cookie
                            (required for list_projects / compile / pdf /
                            create_project).
* ``OVERLEAF_GIT_TOKEN``  — Overleaf Git Integration token (required for
                            read / edit / history / diff operations).
* ``OVERLEAF_REVIEW_PREFIX`` — prepended to every comment-thread reply so
                            co-authors can tell machine-written replies
                            apart. Defaults to ``[auto]``; set it to an
                            empty string to post replies unmarked.
"""

from __future__ import annotations

import os


def get_session() -> str:
    """Return the Overleaf session cookie, or an empty string if unset."""
    return os.environ.get("OVERLEAF_SESSION", "")


def get_git_token() -> str:
    """Return the Overleaf git token, or an empty string if unset."""
    return os.environ.get("OVERLEAF_GIT_TOKEN", "")


def get_review_prefix() -> str:
    """Return the prefix prepended to every comment-thread reply."""
    return os.environ.get("OVERLEAF_REVIEW_PREFIX", "[auto]")
