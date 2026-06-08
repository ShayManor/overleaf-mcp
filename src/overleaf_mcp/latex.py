"""LaTeX document structure parsing utilities."""

from __future__ import annotations

import re
from typing import Any

# Ordered hierarchy from broadest to narrowest
SECTION_LEVELS = [
    "part",
    "chapter",
    "section",
    "subsection",
    "subsubsection",
    "paragraph",
    "subparagraph",
]

# Match the command + ``*`` marker + opening brace. The title that follows
# can contain nested braces (e.g. ``\section{Foo \texttt{bar}}``) so we do
# NOT use the naive ``\{([^}]+)\}`` pattern; instead we locate the opening
# brace and walk the string to find its matching close.
SECTION_PATTERN = re.compile(
    r"\\(" + "|".join(SECTION_LEVELS) + r")(\*?)\s*\{",
    re.MULTILINE,
)


def _extract_braced(content: str, open_idx: int) -> tuple[str, int] | None:
    """Given ``content[open_idx] == '{'``, return (body, index_after_close).

    Handles nested ``{}`` pairs, so ``\\section{Foo \\texttt{bar}}`` yields
    ``Foo \\texttt{bar}``. Backslash-escaped braces are treated as literal.
    Returns ``None`` if no matching close is found.
    """
    if open_idx >= len(content) or content[open_idx] != "{":
        return None
    depth = 1
    i = open_idx + 1
    n = len(content)
    while i < n:
        ch = content[i]
        if ch == "\\" and i + 1 < n:
            # Skip the escaped char (e.g. ``\{``, ``\}``)
            i += 2
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return content[open_idx + 1 : i], i + 1
        i += 1
    return None


def parse_sections(content: str) -> list[dict[str, Any]]:
    """Parse LaTeX content and extract all sectioning commands.

    Returns a list of dicts with keys:
      type, title, preview, start_pos, end_pos, level
    """
    matches = list(SECTION_PATTERN.finditer(content))
    parsed: list[tuple[re.Match, str, int]] = []
    for m in matches:
        open_idx = m.end() - 1  # index of the '{' (SECTION_PATTERN ends on it)
        extracted = _extract_braced(content, open_idx)
        if extracted is None:
            # Malformed — skip this occurrence but continue scanning others.
            continue
        title, after_close = extracted
        parsed.append((m, title, after_close))

    sections: list[dict[str, Any]] = []
    for i, (m, title, header_end) in enumerate(parsed):
        sec_type = m.group(1)
        end = parsed[i + 1][0].start() if i + 1 < len(parsed) else len(content)
        body = content[header_end:end].strip()
        preview = body[:200] + "…" if len(body) > 200 else body

        sections.append(
            {
                "type": sec_type,
                "title": title,
                "preview": preview,
                "start_pos": m.start(),
                "end_pos": end,
                "level": SECTION_LEVELS.index(sec_type),
            }
        )

    return sections


def get_section_content(content: str, title: str) -> str | None:
    """Return the full text (including header) of the section with *title*."""
    for sec in parse_sections(content):
        if sec["title"].lower() == title.lower():
            return content[sec["start_pos"]:sec["end_pos"]]
    return None


def update_section(
    content: str,
    title: str,
    new_body: str,
) -> str | None:
    """Replace the body of the named section, preserving the header.

    Returns the full updated file content, or ``None`` if the section
    was not found.
    """
    sections = parse_sections(content)
    for sec in sections:
        if sec["title"].lower() != title.lower():
            continue

        # Find where the header ends — re-run the balanced-brace walker
        # on the known start position so that titles containing nested
        # braces (e.g. ``\section{Foo \texttt{bar}}``) work correctly.
        start_re = re.compile(
            rf"\\{re.escape(sec['type'])}\*?\s*\{{"
        )
        hm = start_re.search(content, sec["start_pos"])
        if not hm:
            return None

        extracted = _extract_braced(content, hm.end() - 1)
        if extracted is None:
            return None
        _title, header_end = extracted

        return (
            content[:header_end]
            + "\n"
            + new_body.strip()
            + "\n"
            + content[sec["end_pos"]:]
        )

    return None
