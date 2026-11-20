from helpers import example_config, make_candidate

from bed.agents.runner import BudgetExceeded, FixerOutput
from bed.core.fingerprint import fingerprint
from bed.core.models import ExplorationMemory, FileChange, WorkerInput
from bed.testing.fakes import FakeAgentRunner
from bed.worker.pipeline import run_worker
from bed.worker.shell import CmdResult


class Workspace:
    def __init__(self):
        self.files = {}

    def clone(self, *args):
        return "a" * 40

    def install_toolchains(self):
        pass

    def install(self, verify):
        pass

    def verify(self, verify):
        return CmdResult(0, "passed", False)

    def reset(self):
        self.files.clear()

    def usage(self):
        return 0, 0

    def write(self, path, content):
        self.files[path] = content

    def changes(self):
        return [FileChange(path=p, content=c) for p, c in self.files.items()]


def input_for(memory=None, **kw):
    config = example_config()
    return WorkerInput(run_id="r", repo=config.repo("acme/web"), clone_url="u", read_token="t",
                       model=config.model, memory_enabled=True, exploration=memory, **kw)


def test_first_run_explores_and_next_run_fixes_without_scouting():
    candidate = make_candidate()
    scout = FakeAgentRunner([candidate], summary="Small Python service; src/a.py handles users.")
    snapshots = []
    first = run_worker(input_for(), Workspace(), scout, lambda: 0, snapshots.append)
    assert first.status == "ok" and first.exploration.complete and scout.fix_calls == []
    assert first.exploration.summary == scout.summary and snapshots

    def edit(ws):
        ws.write("src/a.py", "guarded = True\n")
        return FixerOutput(status="fixed", summary="guarded missing user")

    fixer = FakeAgentRunner([], {candidate.title: edit}, scout_error=AssertionError("must reuse memory"))
    second = run_worker(input_for(first.exploration), Workspace(), fixer, lambda: 0, snapshots.append)
    assert fixer.scout_calls == 0 and second.results[0].verified
    assert second.exploration.attempted == [fingerprint(candidate)]


def test_exhausted_backlog_triggers_new_exploration_generation():
    candidate = make_candidate()
    old = ExplorationMemory(generation="old", base_sha="b" * 40, summary="existing map", candidates=[candidate],
                            attempted=[fingerprint(candidate)], complete=True)
    scout = FakeAgentRunner([make_candidate(symbol="other")])
    result = run_worker(input_for(old), Workspace(), scout, lambda: 0, lambda r: None)
    assert scout.scout_calls == 1 and result.exploration.generation == "r"
    assert result.exploration.summary == "existing map" and result.exploration.attempted == []


def test_partial_discovery_survives_budget_exhaustion():
    candidate = make_candidate()

    class Scout(FakeAgentRunner):
        def scout(self, ws, inp, discoveries=None):
            discoveries.remember_context("Map saved before the model stops")
            discoveries.remember_finding(candidate)
            raise BudgetExceeded("limit")

    snapshots = []
    result = run_worker(input_for(), Workspace(), Scout([]), lambda: 0, snapshots.append)
    assert result.status == "failed" and result.exploration.candidates == [candidate]
    assert not result.exploration.complete and len(snapshots) == 2
    next_agent = FakeAgentRunner([], scout_error=AssertionError("should not rescout"))
    run_worker(input_for(result.exploration), Workspace(), next_agent, lambda: 0, snapshots.append)
    assert next_agent.fix_calls == [candidate.title] and next_agent.scout_calls == 0


def test_locked_cached_finding_waits_without_rescouting_or_consuming_it():
    candidate = make_candidate()
    memory = ExplorationMemory(generation="g", base_sha="a" * 40, candidates=[candidate], complete=True)
    agent = FakeAgentRunner([], scout_error=AssertionError("should not rescout"))
    result = run_worker(input_for(memory, locked_paths=candidate.files), Workspace(), agent, lambda: 0, lambda r: None)
    assert result.status == "ok" and result.exploration.attempted == [] and agent.scout_calls == 0
