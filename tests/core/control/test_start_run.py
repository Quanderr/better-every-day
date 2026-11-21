import re
from datetime import timedelta

from helpers import NOW, example_config, make_finding

from bed.core.control.start_run import new_run_id, session_id, start_run
from bed.core.models import RunRecord
from bed.core.ports import BotPr
from bed.testing.fakes import FakeCacheGrants, fake_deps


def deps_with(open_prs=0):
    deps = fake_deps(example_config())
    deps.fake_vcs.bot_prs["acme/web"] = [
        BotPr(number=i, url=f"u{i}", state="open", branch=f"b{i}", files=[f"src/f{i}.py"]) for i in range(open_prs)
    ]
    return deps


def test_run_id_format():
    assert re.fullmatch(r"20261117T050000Z-[0-9a-f]{8}", new_run_id(NOW))


def test_session_id_min_length():
    sid = session_id("a/b", new_run_id(NOW))
    assert len(sid) >= 33 and sid.startswith("bed-a-b-")


def test_backpressure_skips():
    deps = deps_with(open_prs=5)
    run = start_run("acme/web", deps)
    assert run.status == "skipped_backpressure"
    assert deps.runtime.started == []
    assert deps.store.get_run("acme/web", run.run_id).status == "skipped_backpressure"


def test_below_cap_starts():
    deps = deps_with(open_prs=4)
    assert start_run("acme/web", deps).status == "started"
    assert len(deps.runtime.started) == 1


def test_running_run_blocks_second():
    deps = deps_with()
    start_run("acme/web", deps)
    second = start_run("acme/web", deps)
    assert second.status == "skipped_running" and len(deps.runtime.started) == 1


def test_stale_started_run_does_not_block():
    deps = deps_with()
    deps.store.put_run(RunRecord(run_id="old", repo="acme/web", status="started",
                                 started_at=NOW - timedelta(minutes=61)))
    assert start_run("acme/web", deps).status == "started"


def test_worker_input_contents():
    deps = fake_deps(example_config())
    deps.store.put_finding(make_finding("fp-merged", status="merged", title="m",
                                        updated_at=NOW - timedelta(hours=1)))
    deps.store.put_finding(make_finding("fp-closed", status="closed", title="c", close_comment="nope",
                                        updated_at=NOW - timedelta(hours=2)))
    deps.store.put_finding(make_finding("fp-open", status="published", pr_number=3))
    deps.store.put_lessons("acme/web", "avoid docs-only PRs")
    deps.fake_vcs.bot_prs["acme/web"] = [BotPr(number=3, url="u", state="open", branch="b", files=["src/open.py"])]
    run = start_run("acme/web", deps)
    sid, payload = deps.runtime.started[0]
    assert sid == session_id("acme/web", run.run_id)
    assert payload.run_id == run.run_id
    assert payload.known_fingerprints == ["fp-merged", "fp-closed", "fp-open"]
    assert payload.locked_paths == ["src/open.py"]
    assert payload.lessons == "avoid docs-only PRs"
    assert [o.state for o in payload.recent_outcomes] == ["merged", "closed"]
    assert payload.recent_outcomes[1].close_comment == "nope"
    assert payload.read_token == "read-token-acme/web-1"
    assert payload.deployment_prompt.startswith("Prefer correctness")


def test_runtime_failure_marks_run_failed():
    deps = deps_with()
    deps.runtime.fail_next("start")
    run = start_run("acme/web", deps)
    assert run.status == "failed" and "injected failure" in run.error
    assert deps.store.get_run("acme/web", run.run_id).status == "failed"


def test_stale_started_run_marked_timeout():
    deps = deps_with()
    deps.store.put_run(RunRecord(run_id="20261117T035000Z-aaaaaaaa", repo="acme/web", status="started",
                                 started_at=NOW - timedelta(minutes=70)))
    start_run("acme/web", deps)
    stale = deps.store.get_run("acme/web", "20261117T035000Z-aaaaaaaa")
    assert stale.status == "timeout" and stale.finished_at == NOW


def test_fix_failed_and_rejected_findings_not_known():
    deps = deps_with()
    deps.store.put_finding(make_finding("fp-failed", status="fix_failed", run_id="old"))
    deps.store.put_finding(make_finding("fp-rej", status="rejected_by_gate", run_id="old"))
    deps.store.put_finding(make_finding("fp-closed", status="closed", run_id="old"))
    start_run("acme/web", deps)
    assert deps.runtime.started[0][1].known_fingerprints == ["fp-closed"]


def test_concurrent_start_blocked_by_lease():
    deps = deps_with()
    assert deps.store.try_lease("start#acme/web", NOW, timedelta(minutes=5))
    run = start_run("acme/web", deps)
    assert run.status == "skipped_running" and deps.runtime.started == []


def test_expired_lease_allows_start():
    deps = deps_with()
    deps.store.try_lease("start#acme/web", NOW - timedelta(minutes=10), timedelta(minutes=5))
    assert start_run("acme/web", deps).status == "started"


def test_nonce_generated_and_sent():
    deps = deps_with()
    run = start_run("acme/web", deps)
    payload = deps.runtime.started[0][1]
    assert len(run.nonce) == 32 and payload.nonce == run.nonce
    assert deps.store.get_run("acme/web", run.run_id).read_token_id == "tok-1"


def test_timeout_marking_revokes_read_token():
    deps = deps_with()
    deps.store.put_run(RunRecord(run_id="20261117T030000Z-aaaaaaaa", repo="acme/web", status="started",
                                 started_at=NOW - timedelta(hours=2), read_token_id="tok-old"))
    start_run("acme/web", deps)
    assert ("acme/web", "tok-old") in deps.fake_vcs.revoked


def test_cache_grant_passed_to_worker_when_enabled():
    deps = fake_deps(example_config())
    deps.cache = FakeCacheGrants()
    start_run("acme/web", deps)
    assert deps.runtime.started[0][1].cache.key == "repos/acme/web"


def test_cache_grant_skipped_when_repo_opts_out_or_no_cache_configured():
    cfg = example_config()
    repos = [r.model_copy(update={"cache": False}) for r in cfg.repos]
    deps = fake_deps(cfg.model_copy(update={"repos": repos}))
    deps.cache = FakeCacheGrants()
    start_run("acme/web", deps)
    assert deps.runtime.started[0][1].cache is None and deps.cache.granted == []
    plain = fake_deps(example_config())
    start_run("acme/web", plain)
    assert plain.runtime.started[0][1].cache is None


def test_cache_grant_failure_does_not_block_run():
    deps = fake_deps(example_config())
    deps.cache = FakeCacheGrants()
    deps.cache.fail_next("grant")
    run = start_run("acme/web", deps)
    assert run.status == "started" and deps.runtime.started[0][1].cache is None
