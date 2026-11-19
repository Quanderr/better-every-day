import hashlib
import hmac
import json
import logging
import re
from datetime import timedelta

from bed.core.control.deps import ControlDeps
from bed.core.control.start_run import revoke_read_token
from bed.core.fingerprint import fingerprint
from bed.core.gates import GateDecision, evaluate, is_safe_path
from bed.core.models import (
    BLOCKING_STATUSES,
    BOT_LABEL,
    Candidate,
    Category,
    Finding,
    FindingStatus,
    FixResult,
    RepoConfig,
    RunRecord,
    WorkerResult,
)
from bed.core.ports import TicketRef

log = logging.getLogger(__name__)
SHA = re.compile(r"[0-9a-f]{40}")
LATE_RESULT_WINDOW = timedelta(hours=24)
LESSONS_MAX_CHARS = 2000
LESSONS_MAX_LINES = 10
ZW = "\u200b"
CI_SKIP = re.compile(r"\[(?:skip ci|ci skip|no ci|skip actions|actions skip)\]|\*\*\*NO_CI\*\*\*", re.IGNORECASE)
TRAILER = re.compile(r"(?im)^\s*(?:co-authored-by|signed-off-by|reviewed-by|acked-by|change-id|skip-checks)\s*:.*$\n?")
CLOSING = re.compile(r"(?i)\b(close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b(?=\s*:?\s*\S*#\d)")
SLASH_COMMAND = re.compile(r"(?m)^(\s*)/")
SUBJECT_MAX = 72
BRANCH_TITLE_MAX = 40

COMMIT_TYPE: dict[Category, str] = {
    "bug": "fix",
    "error-handling": "fix",
    "test-gap": "test",
    "dead-code": "refactor",
    "perf": "perf",
    "docs": "docs",
}


def branch_name(ticket_id: str, title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:BRANCH_TITLE_MAX].rstrip("-")
    return f"{ticket_id}-{slug}"


def _scrub(text: str) -> str:
    return CLOSING.sub(lambda m: m.group(1) + ZW, TRAILER.sub("", CI_SKIP.sub("", text)))


def commit_subject(ticket_id: str, candidate: Candidate) -> str:
    title = " ".join(_scrub(candidate.title).split())
    title = title[:1].lower() + title[1:]
    subject = f"[{ticket_id}] {COMMIT_TYPE[candidate.category]}: {title}"
    return subject[:SUBJECT_MAX].rstrip()


def commit_message(ticket_id: str, candidate: Candidate, summary: str) -> str:
    return f"{commit_subject(ticket_id, candidate)}\n\n{_scrub(summary).strip()}"


def neutralise(text: str) -> str:
    escaped = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
               .replace("@", "@" + ZW).replace("![", "!" + ZW + "["))
    return SLASH_COMMAND.sub(lambda m: m.group(1) + ZW + "/", CLOSING.sub(lambda m: m.group(1) + ZW, escaped))


def fenced(text: str) -> str:
    return "```text\n" + text.replace("```", "``\u200b`") + "\n```"


def ticket_body(result: FixResult) -> str:
    c = result.candidate
    files = "\n".join(f"- `{neutralise(f)}`" for f in c.files)
    return (
        f"Category: {c.category}\n\n"
        f"## Problem\n{fenced(c.evidence)}\n\n"
        f"## Why it matters\n{neutralise(c.rationale)}\n\n"
        f"## Files\n{files}\n\n"
        f"## Proposed fix\n{neutralise(result.summary)}\n"
    )


def pr_body(result: FixResult, ticket: TicketRef, repo: RepoConfig) -> str:
    c = result.candidate
    if repo.verify.mode == "ci":
        verification = "Not run locally; validated by CI."
    else:
        steps = ([repo.verify.install] if repo.verify.install else []) + repo.verify.commands
        verification = "\n".join(f"- `{cmd}` passed" for cmd in steps)
    return (
        f"## What\n{neutralise(result.summary)}\n\n"
        f"## Why\n{fenced(c.evidence)}\n\n{neutralise(c.rationale)}\n\n"
        f"## Verification\n{verification}\n\n"
        f"## Ticket\n[{ticket.id}]({ticket.url})\n\n"
        f"Opened automatically by better-every-day.\n"
    )


def handle_result(result: WorkerResult, deps: ControlDeps) -> RunRecord:
    run = deps.store.get_run(result.repo, result.run_id)
    if run is None:
        return RunRecord(run_id=result.run_id, repo=result.repo, status="failed", started_at=deps.clock(),
                         error="unknown run")
    if not hmac.compare_digest(run.nonce, result.nonce):
        log.warning("ignoring result for %s with a wrong nonce", run.run_id)
        return run
    if run.status not in ("started", "timeout"):
        return run
    if run.status == "timeout" and deps.clock() - run.started_at > LATE_RESULT_WINDOW:
        return run
    repo = deps.config.repo(result.repo)
    if result.exploration is not None and deps.memory is not None:
        memory = result.exploration
        if memory.base_sha != result.base_sha or not SHA.fullmatch(memory.base_sha):
            raise ValueError("invalid exploration base commit")
        if not deps.vcs(repo).is_ancestor(repo.name, memory.base_sha, repo.base_branch):
            raise ValueError("untrusted exploration base commit")
        if len(memory.model_dump_json().encode()) > 60_000:
            raise ValueError("exploration checkpoint too large")
        if any(not is_safe_path(p) for c in memory.candidates for p in c.files):
            raise ValueError("unsafe cached candidate path")
        deps.memory.put(repo.name, memory, run.run_id)
    if result.kind == "checkpoint":
        # A progress event must not finish the run or revoke its live clone token.
        return run
    try:
        return _handle(run, result, repo, deps)
    finally:
        revoke_read_token(run, repo, deps)


def _handle(run: RunRecord, result: WorkerResult, repo: RepoConfig, deps: ControlDeps) -> RunRecord:
    if result.status != "ok":
        return _finish(run, result, deps)
    vcs = deps.vcs(repo)
    if not SHA.fullmatch(result.base_sha) or not vcs.is_ancestor(repo.name, result.base_sha, repo.base_branch):
        rejected = result.model_copy(update={"status": "failed", "error": "untrusted base_sha"})
        return _finish(run, rejected, deps)
    ledger = {f.fingerprint: f for f in deps.store.list_findings(repo.name)}
    known = {fp for fp, f in ledger.items() if f.run_id != run.run_id and f.status in BLOCKING_STATUSES}
    locked = {p for pr in vcs.list_bot_prs(repo.name) if pr.state == "open" for p in pr.files}
    published = 0
    seen: set[str] = set()
    for worker_fix in result.results:
        if published >= repo.limits.max_prs_per_run:
            break
        fix = worker_fix.model_copy(update={"fingerprint": fingerprint(worker_fix.candidate)})
        if fix.fingerprint in seen:
            continue
        seen.add(fix.fingerprint)
        try:
            if _process(fix, result.base_sha, repo, run, ledger, known, locked, deps):
                locked.update(c.path for c in fix.changes)
                published += 1
        except Exception as e:
            _mark_publish_failed(fix, repo, run.run_id, repr(e), deps)
    if result.lessons is not None:
        deps.store.put_lessons(repo.name, cap_lessons(result.lessons))
    return _finish(run, result, deps)


def cap_lessons(text: str) -> str:
    lines = [line for line in text.splitlines() if line.strip()][:LESSONS_MAX_LINES]
    return "\n".join(lines)[:LESSONS_MAX_CHARS]


def _process(fix: FixResult, base_sha: str, repo: RepoConfig, run: RunRecord, ledger: dict[str, Finding],
             known: set[str], locked: set[str], deps: ControlDeps) -> bool:
    existing = ledger.get(fix.fingerprint)
    if existing and existing.run_id == run.run_id and existing.status in ("publishing", "published"):
        publish_one(fix, base_sha, repo, run.run_id, deps)
        return True
    if fix.status != "fixed":
        _record(fix, repo, run.run_id, "fix_failed", fix.status, deps)
        return False
    if fix.fingerprint in known:
        _record(fix, repo, run.run_id, "rejected_by_gate", "duplicate", deps)
        return False
    vcs = deps.vcs(repo)
    base = {c.path: vcs.read_file(repo.name, base_sha, c.path) for c in fix.changes if is_safe_path(c.path)}
    decision: GateDecision = evaluate(fix, repo, known, locked, base)
    if not decision.ok:
        _record(fix, repo, run.run_id, "rejected_by_gate", decision.reason, deps)
        return False
    publish_one(fix, base_sha, repo, run.run_id, deps)
    return True


def _mark_publish_failed(fix: FixResult, repo: RepoConfig, run_id: str, error: str, deps: ControlDeps) -> None:
    current = deps.store.get_finding(repo.name, fix.fingerprint)
    if current is not None and current.run_id != run_id and current.status in BLOCKING_STATUSES:
        return
    if current is None or current.run_id != run_id:
        current = _new_finding(fix, repo, run_id, "publish_failed", None, deps)
    deps.store.put_finding(current.model_copy(update={"status": "publish_failed", "reason": error[:500],
                                                      "updated_at": deps.clock()}))


def publish_one(result: FixResult, base_sha: str, repo: RepoConfig, run_id: str, deps: ControlDeps) -> Finding:
    vcs = deps.vcs(repo)
    tracker = deps.tracker(repo)
    existing = deps.store.get_finding(repo.name, result.fingerprint)
    same_run = existing is not None and existing.run_id == run_id
    if same_run and existing.status == "published":
        return existing
    if same_run and existing.status == "publishing":
        if existing.changes_hash != changes_hash(result):
            raise ValueError("changes differ from the first delivery of this finding")
        finding = existing
    else:
        carry: dict = {}
        if existing is not None and existing.status == "publish_failed":
            carry = {"ticket_id": existing.ticket_id, "ticket_url": existing.ticket_url}
            if existing.changes_hash == changes_hash(result):
                carry.update(branch=existing.branch, pr_number=existing.pr_number, pr_url=existing.pr_url)
            elif existing.branch:
                carry["branch"] = f"{existing.branch}-{run_id.rsplit('-', 1)[-1]}"
        finding = _new_finding(result, repo, run_id, "publishing", None, deps).model_copy(update=carry)
        deps.store.put_finding(finding)
    c = result.candidate

    def save(**update) -> Finding:
        updated = finding.model_copy(update={**update, "updated_at": deps.clock()})
        deps.store.put_finding(updated)
        return updated

    if not finding.ticket_id:
        ticket = tracker.find_ticket(repo.tracker_project, result.fingerprint) or tracker.create_ticket(
            repo.tracker_project, c.title, ticket_body(result), result.fingerprint
        )
        finding = save(ticket_id=ticket.id, ticket_url=ticket.url)
    ticket = TicketRef(id=finding.ticket_id, url=finding.ticket_url or "")
    branch = finding.branch or branch_name(ticket.id, c.title)
    if vcs.find_branch(repo.name, branch) is None:
        vcs.push_branch(repo.name, branch, base_sha, commit_message(ticket.id, c, result.summary), result.changes)
    if finding.branch != branch:
        finding = save(branch=branch)
    if finding.pr_number is None:
        pr = vcs.find_pr(repo.name, branch) or vcs.open_pr(
            repo.name, repo.base_branch, branch, commit_subject(ticket.id, c), pr_body(result, ticket, repo),
            [BOT_LABEL],
        )
        finding = save(pr_number=pr.number, pr_url=pr.url)
    vcs.add_labels(repo.name, finding.pr_number, [BOT_LABEL])
    tracker.link_pr(ticket.id, finding.pr_url or "", commit_subject(ticket.id, c))
    return save(status="published", reason=None)


def _new_finding(result: FixResult, repo: RepoConfig, run_id: str, status: FindingStatus, reason: str | None,
                 deps: ControlDeps) -> Finding:
    now = deps.clock()
    c = result.candidate
    return Finding(repo=repo.name, fingerprint=result.fingerprint, title=c.title, category=c.category,
                   files=sorted({ch.path for ch in result.changes} or set(c.files)), status=status, reason=reason,
                   changes_hash=changes_hash(result), run_id=run_id, created_at=now, updated_at=now)


def changes_hash(result: FixResult) -> str:
    payload = json.dumps([[c.path, c.content] for c in sorted(result.changes, key=lambda c: c.path)])
    return hashlib.sha256(payload.encode()).hexdigest()


def _record(result: FixResult, repo: RepoConfig, run_id: str, status: FindingStatus, reason: str | None,
            deps: ControlDeps) -> None:
    existing = deps.store.get_finding(repo.name, result.fingerprint)
    if existing and existing.status in BLOCKING_STATUSES:
        return
    deps.store.put_finding(_new_finding(result, repo, run_id, status, reason, deps))


def _finish(run: RunRecord, result: WorkerResult, deps: ControlDeps) -> RunRecord:
    finished = run.model_copy(update={"status": result.status, "error": result.error,
                                      "tokens_used": result.tokens_used, "cost_usd": result.cost_usd,
                                      "finished_at": deps.clock()})
    deps.store.put_run(finished)
    return finished
