import hmac
import logging
from datetime import timedelta

from bed.core.budget import DailyBudget
from bed.core.control.deps import ControlDeps
from bed.core.control.publish import is_trusted_base, publish_results
from bed.core.control.start_run import revoke_read_token
from bed.core.gates import is_safe_path
from bed.core.models import (
    EXPLORATION_MAX_BYTES,
    Checkpoint,
    ExplorationMemory,
    RepoConfig,
    RunRecord,
    WorkerResult,
)

log = logging.getLogger(__name__)
LATE_RESULT_WINDOW = timedelta(hours=24)


def handle_result(message: WorkerResult | Checkpoint, deps: ControlDeps) -> RunRecord:
    run = deps.store.get_run(message.repo, message.run_id)
    if run is None:
        return RunRecord(run_id=message.run_id, repo=message.repo, status="failed", started_at=deps.clock(),
                         error="unknown run")
    if not _accepts(run, message.nonce, deps):
        return run
    repo = deps.config.repo(message.repo)
    _save_exploration(run, message, repo, deps)
    if isinstance(message, Checkpoint):
        # A progress event must not finish the run or revoke its live clone token.
        return _progress(run, message, deps)
    try:
        return _finish(run, publish_results(run, message, repo, deps), deps)
    finally:
        revoke_read_token(run, repo, deps)


def _accepts(run: RunRecord, nonce: str, deps: ControlDeps) -> bool:
    if not hmac.compare_digest(run.nonce, nonce):
        log.warning("ignoring result for %s with a wrong nonce", run.run_id)
        return False
    if run.status == "timeout":
        return deps.clock() - run.started_at <= LATE_RESULT_WINDOW
    return run.status == "started"


def _exploration_defect(memory: ExplorationMemory, base_sha: str, repo: RepoConfig, deps: ControlDeps) -> str | None:
    if memory.base_sha != base_sha or not is_trusted_base(memory.base_sha, repo, deps):
        return "untrusted exploration base commit"
    if len(memory.model_dump_json().encode()) > EXPLORATION_MAX_BYTES:
        return "exploration checkpoint too large"
    if any(not is_safe_path(p) for c in memory.candidates for p in c.files):
        return "unsafe cached candidate path"
    return None


def _save_exploration(run: RunRecord, message: WorkerResult | Checkpoint, repo: RepoConfig,
                      deps: ControlDeps) -> None:
    memory = message.exploration
    if memory is None or deps.memory is None:
        return
    defect = _exploration_defect(memory, message.base_sha, repo, deps)
    if defect:
        log.warning("discarding exploration snapshot of run %s: %s", run.run_id, defect)
        return
    deps.memory.put(repo.name, memory, run.run_id)


def _progress(run: RunRecord, checkpoint: Checkpoint, deps: ControlDeps) -> RunRecord:
    progress = run.model_copy(update={"cost_usd": max(run.cost_usd, checkpoint.cost_usd),
                                      "tokens_used": max(run.tokens_used, checkpoint.tokens_used)})
    deps.store.put_run(progress)
    return progress


def _finish(run: RunRecord, result: WorkerResult, deps: ControlDeps) -> RunRecord:
    cost = max(run.cost_usd, result.cost_usd)
    if run.budget_day is not None:
        DailyBudget(deps.store, run.repo, run.budget_day).settle(run.run_id, cost)
    finished = run.model_copy(update={"status": result.status, "error": result.error,
                                      "tokens_used": result.tokens_used, "cost_usd": cost,
                                      "finished_at": deps.clock()})
    deps.store.put_run(finished)
    return finished
