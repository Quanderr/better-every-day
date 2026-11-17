import logging
import re
import secrets
from datetime import datetime, timedelta

from bed.core.control.deps import ControlDeps
from bed.core.control.outcomes import sync_outcomes
from bed.core.models import BLOCKING_STATUSES, OutcomeDigest, RepoConfig, RunRecord, RunStatus, WorkerInput
from bed.core.ports import BotPr

log = logging.getLogger(__name__)
RECENT_OUTCOMES = 20
SESSION_ID_MIN = 33
STALE_LOOKBACK = timedelta(days=2)
START_LEASE = timedelta(minutes=5)


def new_run_id(now: datetime) -> str:
    return f"{now.strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"


def session_id(repo: str, run_id: str) -> str:
    sid = f"bed-{re.sub(r'[^A-Za-z0-9]', '-', repo)}-{run_id}"
    return sid.ljust(SESSION_ID_MIN, "0")


def start_run(repo_name: str, deps: ControlDeps) -> RunRecord:
    repo = deps.config.repo(repo_name)
    now = deps.clock()
    run_id = new_run_id(now)
    prs = sync_outcomes(repo, deps)

    def finish(status: RunStatus, error: str | None = None) -> RunRecord:
        run = RunRecord(run_id=run_id, repo=repo.name, status=status, started_at=now,
                        finished_at=deps.clock() if status != "started" else None, error=error)
        deps.store.put_run(run)
        return run

    if not deps.store.try_lease(f"start#{repo.name}", now, START_LEASE) or active_runs(repo, deps, now):
        return finish("skipped_running")
    open_prs = [pr for pr in prs if pr.state == "open"]
    if len(open_prs) >= repo.limits.max_open_bot_prs:
        return finish("skipped_backpressure")
    nonce = secrets.token_hex(16)
    payload, token_id = _worker_input(run_id, repo, open_prs, nonce, deps)
    started = finish("started").model_copy(update={"nonce": nonce, "read_token_id": token_id})
    deps.store.put_run(started)
    try:
        deps.runtime.start(session_id(repo.name, run_id), payload)
    except Exception as e:
        revoke_read_token(started, repo, deps)
        failed = started.model_copy(update={"status": "failed", "error": repr(e), "finished_at": deps.clock()})
        deps.store.put_run(failed)
        return failed
    return started


def revoke_read_token(run: RunRecord, repo: RepoConfig, deps: ControlDeps) -> None:
    if not run.read_token_id:
        return
    try:
        deps.vcs(repo).revoke_read_token(repo.name, run.read_token_id)
    except Exception:
        log.exception("could not revoke read token for run %s", run.run_id)


def active_runs(repo: RepoConfig, deps: ControlDeps, now: datetime) -> list[RunRecord]:
    deadline = now - timedelta(minutes=repo.limits.timeout_minutes)
    active = []
    for r in deps.store.list_runs(repo.name, now - STALE_LOOKBACK):
        if r.status != "started":
            continue
        if r.started_at > deadline:
            active.append(r)
        else:
            deps.store.put_run(r.model_copy(update={"status": "timeout", "finished_at": now}))
            revoke_read_token(r, repo, deps)
    return active


def _worker_input(run_id: str, repo: RepoConfig, open_prs: list[BotPr], nonce: str,
                  deps: ControlDeps) -> tuple[WorkerInput, str | None]:
    vcs = deps.vcs(repo)
    findings = deps.store.list_findings(repo.name)
    outcomes = sorted((f for f in findings if f.status in ("merged", "closed")),
                      key=lambda f: f.updated_at, reverse=True)[:RECENT_OUTCOMES]
    locked = sorted({path for pr in open_prs for path in pr.files})
    token = vcs.read_token(repo.name)
    return WorkerInput(
        run_id=run_id,
        nonce=nonce,
        repo=repo,
        clone_url=vcs.clone_url(repo.name),
        read_token=token.token,
        model=deps.config.model,
        deployment_prompt=deps.config.prompt,
        known_fingerprints=[f.fingerprint for f in findings if f.status in BLOCKING_STATUSES],
        locked_paths=locked,
        lessons=deps.store.get_lessons(repo.name),
        recent_outcomes=[
            OutcomeDigest(title=f.title, category=f.category, state=f.status, close_comment=f.close_comment or "")
            for f in outcomes
        ],
    ), token.revoke_id
