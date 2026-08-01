"""Offline tests for the verify_citations tool's orchestration.

The verifier CORE lives in ``tofu_search.verify`` (single source of truth,
shared with Tofu's paper mode). These tests prove overleaf_mcp.verify wires it
correctly: a fixture ``.bib`` with a known-bad DOI + a legit entry + a book is
parsed, verified, and the SUSPICIOUS bucket surfaces exactly the bad entry WITH
its bibtex key — while the book stays ``unverifiable`` (never "fake").

The HTTP seam (``tofu_search.search.vertical.base.http_get``) is mocked, so
NOTHING here touches the network. A source-level negative control proves the
suspicious-gating is load-bearing.
"""

import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Skip the whole module if tofu_search.verify isn't importable (stale install)
# — the tool degrades gracefully there, and that path is unit-tested separately.
pytest.importorskip("tofu_search.verify")

import overleaf_mcp.verify as V  # noqa: E402

_BAD_DOI = "10.9999/totally-made-up"
_GOOD_DOI = "10.1145/3292500.3330701"

FIXTURE_BIB = r"""
@inproceedings{good2019,
  title = {A Real, Indexed Conference Paper},
  author = {Doe, Jane and Smith, John},
  year = {2019},
  doi = {%s}
}
@article{ghost2023,
  title = {A Paper That Does Not Exist},
  author = {Phantom, P.},
  year = {2023},
  doi = {%s}
}
@book{cormen2009,
  title = {Introduction to Algorithms},
  author = {Cormen, Thomas H.},
  publisher = {MIT Press},
  year = {2009}
}
""" % (_GOOD_DOI, _BAD_DOI)


class _FakeResp:
    def __init__(self, *, status=200, json_data=None, text=""):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


def _router(url, **kw):
    """DOI resolves: good → CrossRef record; bad → 404; title search → empty."""
    if f"works/{_GOOD_DOI}" in url:
        return _FakeResp(json_data={"message": {"title": ["A Real, Indexed Conference Paper"]}})
    if f"works/{_BAD_DOI}" in url:
        return _FakeResp(status=404)
    if "api.crossref.org/works" in url:        # Tier-2 title search (the book)
        return _FakeResp(json_data={"message": {"items": []}})
    if "semanticscholar.org" in url:           # Tier-2 S2 fallback (the book)
        return _FakeResp(json_data={"data": []})
    return _FakeResp(status=404)


@pytest.fixture
def patch_http(monkeypatch):
    from tofu_search.search.vertical import base
    monkeypatch.setattr(base, "http_get", _router)


def test_run_verification_surfaces_only_the_bad_doi(patch_http):
    result = V.run_verification(FIXTURE_BIB)
    assert result["total"] == 3
    assert result["counts"]["suspicious"] == 1, result["counts"]
    assert result["counts"]["verified"] == 1, result["counts"]
    assert result["counts"]["unverifiable"] == 1, result["counts"]

    susp = result["suspicious"]
    assert len(susp) == 1
    # The suspicious entry carries its bibtex KEY + identifier + reason.
    assert susp[0]["key"] == "ghost2023"
    assert _BAD_DOI in susp[0]["identifier"]
    assert susp[0]["kind"] == "DOI"
    assert susp[0]["reason"]            # non-empty "why"
    # The book stays in the unverifiable bucket — never "fake".
    assert "cormen2009" in result["unverifiable_keys"]
    # The good entry is NOT suspicious.
    assert all(s["key"] != "good2019" for s in susp)


def test_format_report_lists_key_and_separates_unverifiable(patch_http):
    result = V.run_verification(FIXTURE_BIB)
    text = V.format_report(result, ["references.bib"])
    assert "ghost2023" in text
    assert _BAD_DOI in text
    assert "references.bib" in text
    # The disclaimer that unverifiable != fabricated must be present.
    assert "NOT evidence of fabrication" in text
    # The book key must NOT appear in the suspicious section.
    assert "cormen2009" not in text


def test_collect_bibtex_concatenates_and_skips_unreadable():
    def _read(p):
        if p == "broken.bib":
            raise FileNotFoundError(p)
        return "@article{k, title={X}, doi={10.1/y}}"
    combined, read = V.collect_bibtex(_read, ["a.bib", "broken.bib", "b.bib"])
    assert read == ["a.bib", "b.bib"]      # broken.bib skipped
    assert combined.count("@article") == 2


def test_no_suspicious_when_all_resolve(monkeypatch):
    def _all_ok(url, **kw):
        if "api.crossref.org/works/" in url:
            return _FakeResp(json_data={"message": {"title": ["A Matching Title"]}})
        return _FakeResp(status=404)
    from tofu_search.search.vertical import base
    monkeypatch.setattr(base, "http_get", _all_ok)
    bib = "@article{ok, title={A Matching Title}, doi={%s}}" % _GOOD_DOI
    result = V.run_verification(bib)
    assert result["counts"]["suspicious"] == 0
    assert result["suspicious"] == []
    text = V.format_report(result, ["r.bib"])
    assert "No suspicious citations found." in text


# ── SOURCE-LEVEL NEGATIVE CONTROL ────────────────────────────────────────────
# Prove the suspicious-gating is load-bearing: monkeypatch the upstream
# `summarize` (which run_verification calls) so it reports has_suspicious=False
# with an empty suspicious list → the tool surfaces ZERO suspicious entries even
# though the bad DOI is present. Restore → the bad DOI re-appears.

def test_negctl_force_no_suspicious_empties_bucket(patch_http, monkeypatch):
    import tofu_search.verify as TV
    orig = TV.summarize

    def _force_clean(results):
        s = orig(results)
        s["has_suspicious"] = False
        s["suspicious"] = []
        return s

    # baseline: the bad DOI is surfaced
    assert V.run_verification(FIXTURE_BIB)["counts"]["suspicious"] == 1
    # negative control
    monkeypatch.setattr(TV, "summarize", _force_clean)
    forced = V.run_verification(FIXTURE_BIB)
    assert forced["suspicious"] == [], "gating disabled → no suspicious surfaced"
    # restore
    monkeypatch.setattr(TV, "summarize", orig)
    assert V.run_verification(FIXTURE_BIB)["suspicious"][0]["key"] == "ghost2023"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
