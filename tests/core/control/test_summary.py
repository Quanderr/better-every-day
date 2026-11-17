from datetime import timedelta

from helpers import NOW, example_config, make_finding

from bed.core.control.summary import send_summary
from bed.core.models import RunRecord
from bed.testing.fakes import fake_deps


def run(run_id, status, started_at=NOW - timedelta(hours=1), repo="acme/web"):
    finished = None if status == "started" else started_at
    return RunRecord(run_id=run_id, repo=repo, status=status, started_at=started_at, finished_at=finished)


def deps_at_now():
    return fake_deps(example_config())


def test_opened_vs_waiting_split_by_since():
    deps = deps_at_now()
    deps.store.put_finding(make_finding("new", created_at=NOW - timedelta(hours=2)))
    deps.store.put_finding(make_finding("old", created_at=NOW - timedelta(days=3)))
    deps.store.put_finding(make_finding("merged", status="merged", created_at=NOW - timedelta(hours=2)))
    deps.store.put_finding(make_finding("rej", status="rejected_by_gate", created_at=NOW - timedelta(hours=2)))
    s = send_summary(deps)
    assert [f.fingerprint for f in s.opened] == ["new"]
    assert [f.fingerprint for f in s.waiting] == ["old"]
    assert deps.fake_notifier.sent == [s]


def test_since_from_meta():
    deps = deps_at_now()
    deps.store.put_meta("last_summary_at", (NOW - timedelta(days=5)).isoformat())
    deps.store.put_finding(make_finding("x", created_at=NOW - timedelta(days=3)))
    assert [f.fingerprint for f in send_summary(deps).opened] == ["x"]


def test_skipped_and_failed_runs_included_across_repos():
    deps = deps_at_now()
    deps.store.put_run(run("1", "skipped_backpressure"))
    deps.store.put_run(run("2", "baseline_red", repo="acme/api"))
    deps.store.put_run(run("3", "workspace_full"))
    deps.store.put_run(run("4", "ok"))
    deps.store.put_run(run("5", "failed", started_at=NOW - timedelta(days=2)))
    s = send_summary(deps)
    assert [r.run_id for r in s.skipped] == ["1"]
    assert sorted(r.run_id for r in s.failed) == ["2", "3"]


def test_empty_summary_sends_nothing_but_advances():
    deps = deps_at_now()
    s = send_summary(deps)
    assert s.is_empty() and deps.fake_notifier.sent == []
    assert deps.store.get_meta("last_summary_at") == NOW.isoformat()


def test_no_notifier_configured_does_not_raise():
    cfg = example_config().model_copy(update={"notifications": None})
    deps = fake_deps(cfg)
    deps.store.put_finding(make_finding("new", created_at=NOW - timedelta(hours=1)))
    assert send_summary(deps).opened
    assert deps.fake_notifier.sent == []


def test_second_call_sends_nothing_new():
    deps = deps_at_now()
    deps.store.put_run(run("1", "failed"))
    send_summary(deps)
    deps.clock.now = NOW + timedelta(minutes=1)
    second = send_summary(deps)
    assert second.is_empty() and len(deps.fake_notifier.sent) == 1


def test_waiting_only_does_not_send():
    deps = deps_at_now()
    deps.store.put_finding(make_finding("old", created_at=NOW - timedelta(days=3)))
    s = send_summary(deps)
    assert [f.fingerprint for f in s.waiting] == ["old"] and deps.fake_notifier.sent == []


def test_run_started_before_since_finished_after_is_reported():
    deps = deps_at_now()
    deps.store.put_meta("last_summary_at", (NOW - timedelta(hours=1)).isoformat())
    r = run("x", "baseline_red", started_at=NOW - timedelta(hours=2)).model_copy(
        update={"finished_at": NOW - timedelta(minutes=30)})
    deps.store.put_run(r)
    assert [x.run_id for x in send_summary(deps).failed] == ["x"]


def test_run_finished_before_since_not_reported_again():
    deps = deps_at_now()
    deps.store.put_meta("last_summary_at", (NOW - timedelta(hours=1)).isoformat())
    r = run("x", "failed", started_at=NOW - timedelta(hours=3)).model_copy(
        update={"finished_at": NOW - timedelta(hours=2)})
    deps.store.put_run(r)
    assert send_summary(deps).failed == []


def test_summary_marks_stale_started_run_timeout():
    deps = deps_at_now()
    deps.store.put_run(run("20261117T030000Z-aaaaaaaa", "started", started_at=NOW - timedelta(hours=2)))
    s = send_summary(deps)
    assert [(r.run_id, r.status) for r in s.failed] == [("20261117T030000Z-aaaaaaaa", "timeout")]


def test_stuck_publishing_findings_reported():
    deps = deps_at_now()
    deps.store.put_finding(make_finding("stuck", status="publishing", updated_at=NOW - timedelta(hours=2)))
    deps.store.put_finding(make_finding("fresh", status="publishing", updated_at=NOW - timedelta(minutes=5)))
    s = send_summary(deps)
    assert [f.fingerprint for f in s.stuck] == ["stuck"] and len(deps.fake_notifier.sent) == 1


def test_publish_failed_findings_reported_as_stuck():
    deps = deps_at_now()
    deps.store.put_finding(make_finding("pf", status="publish_failed", updated_at=NOW - timedelta(minutes=5)))
    assert [f.fingerprint for f in send_summary(deps).stuck] == ["pf"]
