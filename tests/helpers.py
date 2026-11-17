from datetime import UTC, datetime
from pathlib import Path

from bed.core.config import load_config
from bed.core.models import Candidate, Finding

NOW = datetime(2026, 11, 17, 5, 0, tzinfo=UTC)
EXAMPLE_TEXT = (Path(__file__).parents[1] / "config" / "example.yaml").read_text()


def example_config():
    return load_config(EXAMPLE_TEXT)


def make_candidate(**kw):
    base = dict(title="Handle missing user", category="bug", files=["src/a.py"], symbol="f",
                issue_kind="null-deref", evidence="user may be None", rationale="crash on logout")
    return Candidate(**{**base, **kw})


def make_finding(fp, status="published", **kw):
    base = dict(repo="acme/web", fingerprint=fp, title="t", category="bug", files=["src/a.py"], status=status,
                run_id="r0", created_at=NOW, updated_at=NOW)
    return Finding(**{**base, **kw})
