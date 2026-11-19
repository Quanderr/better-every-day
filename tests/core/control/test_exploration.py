from datetime import timedelta

from helpers import example_config, make_candidate

from bed.core.control.publish import handle_result
from bed.core.control.start_run import start_run
from bed.core.models import ExplorationMemory, WorkerResult
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
        "usd_budget": 3, "cached_run_usd_budget": 0.75, "total_usd_budget": 4.5, "budget_key": "test"})})
    deps = fake_deps(config.model_copy(update={"repos": [repo]}))
    deps.memory = Memory()
    return deps


def test_checkpoint_is_authenticated_and_does_not_finish_or_revoke():
    deps = deps_with_budget()
    run = start_run("acme/web", deps)
    memory = ExplorationMemory(generation=run.run_id, base_sha="a" * 40, candidates=[make_candidate()])
    checkpoint = WorkerResult(run_id=run.run_id, repo=run.repo, base_sha="a" * 40, status="started",
                              kind="checkpoint", exploration=memory, nonce="wrong")
    handle_result(checkpoint, deps)
    assert deps.memory.value is None
    handle_result(checkpoint.model_copy(update={"nonce": run.nonce}), deps)
    assert deps.memory.value == memory
    assert deps.store.get_run(run.repo, run.run_id).status == "started" and deps.fake_vcs.revoked == []


def test_cached_run_gets_smaller_allowance_and_total_cannot_be_reopened_by_zero_cost_report():
    deps = deps_with_budget()
    first = start_run("acme/web", deps)
    assert first.budget_reserved_usd == 3
    for index in range(3):
        # Completing a run does not refund its reservation based on worker claims.
        previous = list(deps.store.runs.values())[-1]
        deps.store.put_run(previous.model_copy(update={"status": "ok", "cost_usd": 0}))
        deps.clock.now += timedelta(minutes=6)
        deps.memory.value = ExplorationMemory(generation="saved", base_sha="a" * 40,
                                              candidates=[make_candidate()])
        run = start_run("acme/web", deps)
        if index < 2:
            assert run.budget_reserved_usd == 0.75
        else:
            assert run.status == "skipped_budget"
    assert len(deps.runtime.started) == 3
