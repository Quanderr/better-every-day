from datetime import UTC, datetime, timedelta

import pytest

from bed.core.models import Finding, RunRecord
from bed.testing.fakes import FakeTracker, FakeVcs, InMemoryStore

NOW = datetime(2026, 11, 17, 5, 0, tzinfo=UTC)


def run(run_id, started_at, status="ok"):
    return RunRecord(run_id=run_id, repo="acme/web", status=status, started_at=started_at)


def finding(fp):
    return Finding(repo="acme/web", fingerprint=fp, title="t", category="bug", files=["a"], status="published",
                   run_id="r", created_at=NOW, updated_at=NOW)


def test_store_round_trips():
    s = InMemoryStore()
    s.put_run(run("r1", NOW))
    s.put_finding(finding("fp1"))
    s.put_lessons("acme/web", "L")
    s.put_meta("k", "v")
    assert s.get_run("acme/web", "r1").run_id == "r1"
    assert s.get_run("acme/web", "missing") is None
    assert s.get_finding("acme/web", "fp1").fingerprint == "fp1"
    assert [f.fingerprint for f in s.list_findings("acme/web")] == ["fp1"]
    assert s.get_lessons("acme/web") == "L" and s.get_lessons("other") == ""
    assert s.get_meta("k") == "v" and s.get_meta("none") is None


def test_list_runs_filters_by_since():
    s = InMemoryStore()
    s.put_run(run("old", NOW - timedelta(days=2)))
    s.put_run(run("new", NOW))
    assert [r.run_id for r in s.list_runs("acme/web", NOW - timedelta(days=1))] == ["new"]


def test_fail_next_raises_once():
    vcs = FakeVcs()
    vcs.fail_next("open_pr")
    with pytest.raises(RuntimeError):
        vcs.open_pr("acme/web", "main", "b", "t", "body", [])
    assert vcs.open_pr("acme/web", "main", "b", "t", "body", []).number == 1


def test_tracker_find_returns_created():
    t = FakeTracker()
    assert t.find_ticket("WEB", "fp") is None
    ref = t.create_ticket("WEB", "title", "body", "fp")
    assert ref.id == "WEB-1" and t.find_ticket("WEB", "fp") == ref


def test_vcs_branch_and_pr_idempotent_lookup():
    vcs = FakeVcs()
    vcs.push_branch("acme/web", "WEB-1-x", "abc", "msg", [])
    assert vcs.find_branch("acme/web", "WEB-1-x") is not None
    pr = vcs.open_pr("acme/web", "main", "WEB-1-x", "t", "b", ["better-every-day"])
    assert vcs.find_pr("acme/web", "WEB-1-x") == pr


def test_fake_lease():
    s = InMemoryStore()
    assert s.try_lease("k", NOW, timedelta(minutes=5))
    assert not s.try_lease("k", NOW + timedelta(minutes=1), timedelta(minutes=5))
    assert s.try_lease("k", NOW + timedelta(minutes=5, seconds=1), timedelta(minutes=5))
