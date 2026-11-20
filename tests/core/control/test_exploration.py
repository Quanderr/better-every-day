from datetime import timedelta

from helpers import example_config, make_candidate

from bed.core.control.results import handle_result
from bed.core.control.start_run import start_run
from bed.core.models import Checkpoint, ExplorationMemory, WorkerResult
from bed.testing.fakes import fake_deps


class Memory:
    def __init__(self):
        self.value = None

    def get(self, repo):
        return self.value

    def put(self, repo, value, run_id):
        self.value = value


def deps_with_budget():
    config = example_config()
    repo = config.repos[0]
    repo = repo.model_copy(update={"limits": repo.limits.model_copy(update={
        "daily_usd_budget": 3})})
    deps = fake_deps(config.model_copy(update={"repos": [repo]}))
    deps.memory = Memory()
    return deps


def test_checkpoint_is_authenticated_and_does_not_finish_or_revoke():
    deps = deps_with_budget()
    run = start_run("acme/web", deps)
    memory = ExplorationMemory(generation=run.run_id, base_sha="a" * 40, candidates=[make_candidate()])
    checkpoint = Checkpoint(run_id=run.run_id, repo=run.repo, base_sha="a" * 40, exploration=memory,
                            nonce="wrong")
    handle_result(checkpoint, deps)
    assert deps.memory.value is None
    handle_result(checkpoint.model_copy(update={"nonce": run.nonce}), deps)
    assert deps.memory.value == memory
    assert deps.store.get_run(run.repo, run.run_id).status == "started" and deps.fake_vcs.revoked == []


def test_untrusted_snapshot_is_discarded_and_the_run_still_finishes():
    deps = deps_with_budget()
    run = start_run("acme/web", deps)
    deps.fake_vcs.foreign_shas.add("b" * 40)
    memory = ExplorationMemory(generation=run.run_id, base_sha="b" * 40, candidates=[make_candidate()])
    finished = handle_result(WorkerResult(run_id=run.run_id, repo=run.repo, base_sha="b" * 40, status="ok",
                                          nonce=run.nonce, exploration=memory), deps)
    assert deps.memory.value is None
    assert finished.status == "failed" and finished.error == "untrusted base_sha"
    assert deps.fake_vcs.revoked


def finish_run(deps, run, cost, status="ok"):
    return handle_result(WorkerResult(run_id=run.run_id, repo=run.repo, status=status,
                                     nonce=run.nonce, cost_usd=cost), deps)


def test_daily_budget_releases_unused_allowance_and_exhausts():
    deps = deps_with_budget()
    first = start_run("acme/web", deps)
    assert first.budget_reserved_usd == 3
    finish_run(deps, first, 0.5)
    finish_run(deps, first, 0.5)  # duplicate delivery cannot refund twice
    deps.clock.now += timedelta(minutes=6)
    second = start_run("acme/web", deps)
    assert second.budget_reserved_usd == 2.5
    finish_run(deps, second, 2.5, "failed")
    deps.clock.now += timedelta(minutes=6)
    assert start_run("acme/web", deps).status == "skipped_budget"


def test_missing_result_keeps_reservation_until_new_utc_day():
    deps = deps_with_budget()
    first = start_run("acme/web", deps)
    deps.clock.now += timedelta(hours=2)
    assert start_run("acme/web", deps).status == "skipped_budget"
    deps.clock.now += timedelta(days=1)
    second = start_run("acme/web", deps)
    assert second.budget_reserved_usd == 3 and second.budget_day != first.budget_day


def test_late_result_settles_original_day_not_current_day():
    from datetime import UTC, datetime
    deps = deps_with_budget()
    deps.clock.now = datetime(2026, 11, 20, 23, 50, tzinfo=UTC)
    first = start_run("acme/web", deps)
    deps.clock.now += timedelta(minutes=61)
    second = start_run("acme/web", deps)
    assert second.budget_reserved_usd == 3
    finish_run(deps, first, 0.4)
    import json
    current = json.loads(deps.store.get_meta(f"budget#daily#acme/web#{second.budget_day}"))
    assert current == {second.run_id: 3}


def test_budget_date_uses_utc_and_unlimited_does_not_reserve():
    from datetime import datetime, timezone
    deps = deps_with_budget()
    deps.clock.now = datetime(2026, 11, 21, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    first = start_run("acme/web", deps)
    assert first.budget_day == "2026-11-20"
    deps = deps_with_budget()
    repo = deps.config.repos[0]
    repo = repo.model_copy(update={"limits": repo.limits.model_copy(update={"daily_usd_budget": None})})
    deps.config = deps.config.model_copy(update={"repos": [repo]})
    run = start_run("acme/web", deps)
    assert run.budget_day is None and run.budget_reserved_usd == 0
    assert not any(k.startswith("budget#") for k in deps.store.meta)
