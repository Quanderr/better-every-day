import logging
import os
import re
import threading
import time
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from pydantic import ValidationError

from bed.agents.runner import AgentRunner
from bed.core.models import FixResult, WorkerInput, WorkerResult
from bed.core.ports import ResultSink
from bed.worker.depcache import DependencyCache, NoCache
from bed.worker.pipeline import run_worker
from bed.worker.workspace import Workspace

log = logging.getLogger(__name__)
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
_busy = threading.Lock()


def handle_invocation(
    payload: dict,
    sink: ResultSink,
    start_task: Callable[[str], Any],
    complete_task: Callable[[Any], None],
    runner_factory: Callable[[WorkerInput], AgentRunner],
    spawn: Callable[[Callable[[], None]], None],
    workdir_base: Path = Path("/tmp"),
    cache_factory: Callable[[WorkerInput], DependencyCache | NoCache] = lambda inp: NoCache(),
) -> dict:
    try:
        inp = WorkerInput.model_validate(payload)
    except ValidationError as e:
        return {"status": "rejected", "error": _describe(e)}
    if not RUN_ID.fullmatch(inp.run_id):
        return {"status": "rejected", "error": "invalid run_id"}
    if not _busy.acquire(blocking=False):
        return {"status": "busy"}
    try:
        task = start_task("run")
    except Exception as e:
        _busy.release()
        return {"status": "rejected", "error": repr(e)}

    def job() -> None:
        agents = None
        try:
            agents = runner_factory(inp)
            result = run_worker(inp, Workspace(workdir_base / inp.run_id), agents, time.monotonic, sink.emit,
                                cache_factory(inp))
        except Exception as e:
            log.exception("worker job crashed")
            result = WorkerResult(run_id=inp.run_id, repo=inp.repo.name, status="failed", error=repr(e),
                                  nonce=inp.nonce, cost_usd=agents.cost_usd if agents else 0,
                                  tokens_used=agents.tokens_used if agents else 0)
        try:
            _emit(sink, result)
        except Exception:
            log.exception("result emit failed")
        finally:
            complete_task(task)
            _busy.release()

    try:
        spawn(job)
    except Exception as e:
        complete_task(task)
        _busy.release()
        return {"status": "rejected", "error": repr(e)}
    return {"status": "accepted", "run_id": inp.run_id}


def _describe(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in error.errors(include_input=False)
    )


def _change_bytes(fix: FixResult) -> int:
    return sum(len(c.content or "") for c in fix.changes)


def _emit(sink: ResultSink, result: WorkerResult) -> None:
    while True:
        try:
            sink.emit(result)
            return
        except ValueError:
            sized = [(i, _change_bytes(f)) for i, f in enumerate(result.results) if f.changes]
            if not sized:
                break
            index = max(sized, key=lambda item: item[1])[0]
            log.warning("result too large; dropping changes of %s", result.results[index].candidate.title)
            dropped = result.results[index].model_copy(update={
                "status": "fix_failed", "changes": [], "verify_log": "dropped: result too large for the queue",
            })
            result = result.model_copy(update={"results": [
                dropped if i == index else f for i, f in enumerate(result.results)]})
    log.error("result still too large after dropping all changes, sending failure instead")
    sink.emit(WorkerResult(run_id=result.run_id, repo=result.repo, base_sha=result.base_sha, status="failed",
                           tokens_used=result.tokens_used, error="result too large", nonce=result.nonce))


app = BedrockAgentCoreApp()


@lru_cache(maxsize=1)
def _sink() -> ResultSink:
    from bed.platform.aws.eventbus import SqsSink

    return SqsSink(os.environ["BED_RESULT_QUEUE_URL"])


def _runner(inp: WorkerInput) -> AgentRunner:
    from bed.agents.runner import StrandsAgentRunner

    return StrandsAgentRunner(inp)


def _cache(inp: WorkerInput) -> DependencyCache | NoCache:
    if inp.cache is None:
        return NoCache()
    from bed.platform.aws.cache import S3CacheStore

    return DependencyCache(S3CacheStore(inp.cache), Path.home())


def _spawn(job: Callable[[], None]) -> None:
    threading.Thread(target=job, daemon=True).start()


@app.entrypoint
def invoke(payload: dict) -> dict:
    return handle_invocation(payload, _sink(), app.add_async_task, app.complete_async_task, _runner, _spawn,
                             cache_factory=_cache)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run()
