"""Citation verification for an Overleaf project's bibliography.

This module is THIN orchestration only: it locates and reads the project's
``.bib`` file(s) through the existing git_client seam, then delegates ALL
parsing and verification to :mod:`tofu_search.verify` — the single source of
truth shared with Tofu's paper-reading mode. There is deliberately NO second
verifier or threshold table here; if the verification logic changes it changes
once, upstream.

Three-state discipline (inherited from tofu_search.verify):
  - ``verified``     — an authoritative catalogue (CrossRef / arXiv) matches.
  - ``suspicious``   — a concrete DOI/arXiv id definitively fails to resolve
                       (or resolves to an unrelated paper). High-confidence.
  - ``unverifiable`` — could not confirm OR refute (no id, coverage gap, book,
                       dataset, rate-limit). Reported as its OWN bucket, NEVER
                       presented as a fabrication.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("overleaf-mcp")

# Extension used to discover bibliography files in the project tree.
_BIB_EXT = ".bib"


def verify_available() -> tuple[bool, str]:
    """Check that ``tofu_search.verify`` is importable in this environment.

    Returns ``(True, '')`` when usable, else ``(False, <actionable message>)``.
    The published ``tofu-search`` on PyPI may predate the ``verify``
    subpackage, so a stale install imports the package but not the submodule —
    we surface that as a clear upgrade hint rather than a cryptic ImportError.
    """
    try:
        import tofu_search.verify  # noqa: F401
        return True, ""
    except Exception as e:  # ImportError or anything else
        return False, (
            "citation verification needs the `tofu-search` package with the "
            "`verify` module (>=0.4.0). It is not importable: "
            f"{e}. Install/upgrade with `pip install -U 'tofu-search>=0.4.0'`."
        )


def collect_bibtex(read_fn, bib_paths) -> tuple[str, list[str]]:
    """Concatenate the text of every ``.bib`` path via ``read_fn``.

    ``read_fn(path) -> str`` is injected (the caller wires it to
    git_client.read_file bound to a project) so this stays I/O-seam-agnostic
    and trivially testable. Returns ``(combined_text, paths_read)``; a path
    that fails to read is logged and skipped (best-effort).
    """
    chunks: list[str] = []
    read: list[str] = []
    for path in bib_paths:
        try:
            text = read_fn(path)
        except Exception as e:
            logger.warning("[verify] failed to read %s: %s", path, e)
            continue
        if text:
            chunks.append(f"% ── {path} ──\n{text}")
            read.append(path)
    return "\n\n".join(chunks), read


def run_verification(bibtex_text: str) -> dict:
    """Parse + verify a combined BibTeX string via tofu_search.verify.

    Returns a structured dict::

        {
          'total':   <int>,                 # citations parsed
          'counts':  {'verified','suspicious','unverifiable'},
          'suspicious': [ {key, identifier, kind, reason, checked,
                           claimed_title, matched_title}, ... ],
          'unverifiable_keys': [ <bibtex key>, ... ],
        }
    """
    from tofu_search.verify import parse_bibtex, summarize, verify_citations

    citations = parse_bibtex(bibtex_text)
    results = verify_citations(citations)
    summary = summarize(results)

    suspicious = []
    for r in summary["suspicious"]:
        cit = r.get("citation", {})
        ev = r.get("evidence", {})
        identifier = cit.get("doi") or cit.get("arxiv_id") or cit.get("title") or "(unknown)"
        kind = "DOI" if cit.get("doi") else ("arXiv" if cit.get("arxiv_id") else "title")
        suspicious.append({
            "key": cit.get("key", ""),
            "identifier": identifier,
            "kind": kind,
            "reason": ev.get("reason", ""),
            "checked": ev.get("checked", ""),
            "claimed_title": ev.get("claimed_title", ""),
            "matched_title": ev.get("matched_title", ""),
        })

    unverifiable_keys = [
        (r.get("citation", {}) or {}).get("key", "")
        for r in results
        if r.get("state") == "unverifiable"
    ]

    return {
        "total": summary["total"],
        "counts": summary["counts"],
        "suspicious": suspicious,
        "unverifiable_keys": [k for k in unverifiable_keys if k],
    }


def format_report(result: dict, bib_paths: list[str]) -> str:
    """Render the verification result as human-readable text for the tool."""
    counts = result["counts"]
    total = result["total"]
    src = ", ".join(bib_paths) if bib_paths else "(none)"
    head = (
        f"Citation verification — {total} reference(s) from {src}\n"
        f"  ✓ verified: {counts.get('verified', 0)}   "
        f"⚠ suspicious: {counts.get('suspicious', 0)}   "
        f"? unverifiable: {counts.get('unverifiable', 0)}\n"
        "  (unverifiable = could not confirm or refute — NOT evidence of fabrication)\n"
    )
    if not result["suspicious"]:
        return head + "\nNo suspicious citations found."

    lines = [head, "\nSuspicious citations (high-confidence — a concrete identifier did not resolve):"]
    for s in result["suspicious"]:
        key = s["key"] or "(no key)"
        lines.append(f"\n  • [{key}] {s['kind']} {s['identifier']}")
        if s["reason"]:
            lines.append(f"      reason : {s['reason']}")
        if s["claimed_title"] and s["matched_title"]:
            lines.append(f"      claimed: {s['claimed_title']}")
            lines.append(f"      found  : {s['matched_title']}")
        if s["checked"]:
            lines.append(f"      checked: {s['checked']}")
    return "\n".join(lines)
