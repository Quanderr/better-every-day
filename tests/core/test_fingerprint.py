from bed.core.fingerprint import fingerprint
from bed.core.models import Candidate


def candidate(**kw):
    base = dict(
        title="T", category="bug", files=["src/a.py"], symbol="f", issue_kind="null-deref",
        evidence="e", rationale="r",
    )
    return Candidate(**{**base, **kw})


def test_fingerprint_stable_across_title_and_evidence():
    a = candidate(title="A", evidence="x")
    b = candidate(title="B", evidence="y")
    assert fingerprint(a) == fingerprint(b) and len(fingerprint(a)) == 16


def test_fingerprint_normalizes():
    assert fingerprint(candidate(files=["./src/a.py"], issue_kind="Null Deref", symbol=" f ")) == fingerprint(
        candidate(files=["src/a.py"], issue_kind="null-deref")
    )


def test_fingerprint_differs_by_symbol():
    assert fingerprint(candidate(symbol="f")) != fingerprint(candidate(symbol="g"))


def test_fingerprint_differs_by_category():
    assert fingerprint(candidate(category="bug")) != fingerprint(candidate(category="perf"))
