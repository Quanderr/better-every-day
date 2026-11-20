import logging
from collections.abc import Callable

from bed.agents.runner import AgentRunner, run_deadline
from bed.core.fingerprint import fingerprint, normalize_path
from bed.core.models import Candidate, Checkpoint, FixResult, WorkerInput, WorkerResult
from bed.worker.exploration import ExplorationJournal
from bed.worker.workspace import Workspace, WorkspaceError, WorkspaceFull

log = logging.getLogger(__name__)
ERROR_TAIL = 4000
VERIFY_LOG_TAIL = 4000


class _Budget:
    def __init__(self, inp: WorkerInput, agents: AgentRunner, clock: Callable[[], float]):
        self.inp = inp
        self.agents = agents
        self.clock = clock
        self.start = clock()

    def exhausted(self) -> bool:
        limits = self.inp.repo.limits
        if self.agents.budget_blocked or self.agents.tokens_used >= limits.token_budget:
            return True
        return self.clock() >= run_deadline(limits, self.start)


def run_worker(inp: WorkerInput, ws: Workspace, agents: AgentRunner, clock: Callable[[], float],
               checkpoint: Callable[[Checkpoint], None]) -> WorkerResult:
    base_sha = ""
    journal: ExplorationJournal | None = None

    def result(status, results=(), lessons=None, error=None) -> WorkerResult:
        return WorkerResult(run_id=inp.run_id, repo=inp.repo.name, base_sha=base_sha, status=status,
                            results=list(results), lessons=lessons, tokens_used=agents.tokens_used, error=error,
                            nonce=inp.nonce, exploration=journal.memory if journal else None,
                            cost_usd=agents.cost_usd)

    budget = _Budget(inp, agents, clock)
    sandbox = inp.repo.verify.mode == "sandbox"
    try:
        try:
            base_sha = ws.clone(inp.clone_url, inp.read_token, inp.repo.base_branch)
            ws.install_toolchains()
            if sandbox:
                ws.install(inp.repo.verify)
            _log_usage(ws, "after install")
            if sandbox:
                baseline = ws.verify(inp.repo.verify)
                if baseline.code != 0:
                    return result("baseline_red", error=baseline.output[-ERROR_TAIL:])
                ws.reset()
        except WorkspaceFull:
            raise
        except WorkspaceError as e:
            return result("failed", error=str(e)[-ERROR_TAIL:])
        if inp.memory_enabled:
            journal = ExplorationJournal(inp, base_sha, checkpoint, agents)
            if not journal.pending:
                _explore(inp, ws, agents, journal)
                return result("ok")
            log.info("using cached exploration; pending=%d", len(journal.pending))
            candidates = journal.pending
        else:
            candidates = agents.scout(ws, inp).candidates
        results = _fix_all(_filter(candidates, inp), inp, ws, agents, budget, journal)
        lessons = _lessons(inp, agents, budget)
        _log_usage(ws, "at end")
        return result("ok", results, lessons)
    except WorkspaceFull as e:
        return result("workspace_full", error=str(e)[-ERROR_TAIL:])
    except Exception as e:
        log.exception("worker run failed")
        return result("failed", error=repr(e)[-ERROR_TAIL:])


def _explore(inp: WorkerInput, ws: Workspace, agents: AgentRunner, journal: ExplorationJournal) -> None:
    log.info("exploring repository; generation=%s", journal.memory.generation)
    found = agents.scout(ws, inp.model_copy(update={"exploration": journal.memory}), journal)
    for candidate in found.candidates:
        journal.remember_finding(candidate)
    journal.complete(found.summary)


def _lessons(inp: WorkerInput, agents: AgentRunner, budget: _Budget) -> str | None:
    if not inp.recent_outcomes or budget.exhausted():
        return None
    try:
        return agents.lessons(inp)
    except Exception:
        log.exception("lessons generation failed")
        return None


def _filter(candidates: list[Candidate], inp: WorkerInput) -> list[Candidate]:
    known = set(inp.known_fingerprints)
    locked = {normalize_path(p) for p in inp.locked_paths}
    enabled = set(inp.repo.categories)
    out: list[Candidate] = []
    for c in candidates:
        fp = fingerprint(c)
        if fp in known or c.category not in enabled or locked & {normalize_path(f) for f in c.files}:
            continue
        known.add(fp)
        out.append(c)
    return out


def _fix_all(candidates: list[Candidate], inp: WorkerInput, ws: Workspace, agents: AgentRunner,
             budget: _Budget, journal: ExplorationJournal | None) -> list[FixResult]:
    cap = inp.repo.limits.max_prs_per_run
    results: list[FixResult] = []
    for attempt, candidate in enumerate(candidates):
        if attempt >= 2 * cap or sum(r.status == "fixed" for r in results) >= cap or budget.exhausted():
            break
        if journal:
            journal.attempted(candidate)
        results.append(_fix_one(candidate, inp, ws, agents))
        try:
            ws.reset()
        except WorkspaceError:
            log.exception("workspace reset failed; stopping fixes")
            break
    return results


def _fix_one(candidate: Candidate, inp: WorkerInput, ws: Workspace, agents: AgentRunner) -> FixResult:
    fp = fingerprint(candidate)
    try:
        out = agents.fix(ws, inp, candidate)
    except WorkspaceFull:
        raise
    except Exception as e:
        log.exception("fixer failed")
        return FixResult(candidate=candidate, fingerprint=fp, status="fix_failed", verify_log=repr(e))
    if out.status != "fixed":
        return FixResult(candidate=candidate, fingerprint=fp, status=out.status, summary=out.summary)
    verified = False
    verify_log = ""
    if inp.repo.verify.mode == "sandbox":
        check = ws.verify(inp.repo.verify)
        verified = check.code == 0
        verify_log = check.output[-VERIFY_LOG_TAIL:]
    status = "fixed" if verified or inp.repo.verify.mode == "ci" else "fix_failed"
    try:
        changes = ws.changes()
    except WorkspaceError as e:
        return FixResult(candidate=candidate, fingerprint=fp, status="fix_failed", summary=out.summary,
                         verify_log=str(e))
    return FixResult(candidate=candidate, fingerprint=fp, status=status, changes=changes, summary=out.summary,
                     verify_log=verify_log, verified=verified)


def _log_usage(ws: Workspace, when: str) -> None:
    size, count = ws.usage()
    log.info("workspace usage %s: bytes=%d files=%d", when, size, count)
