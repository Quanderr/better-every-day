from datetime import datetime, timedelta

from bed.core.control.deps import ControlDeps
from bed.core.control.start_run import STALE_LOOKBACK, active_runs
from bed.core.ports import Summary

LAST_SUMMARY_KEY = "last_summary_at"
FAILED_STATUSES = {"failed", "baseline_red", "workspace_full", "timeout"}
STUCK_AFTER = timedelta(hours=1)


def send_summary(deps: ControlDeps) -> Summary:
    now = deps.clock()
    stored = deps.store.get_meta(LAST_SUMMARY_KEY)
    since = datetime.fromisoformat(stored) if stored else now - timedelta(hours=24)
    summary = Summary()
    for repo in deps.config.repos:
        for f in deps.store.list_findings(repo.name):
            stuck_publishing = f.status == "publishing" and f.updated_at <= now - STUCK_AFTER
            if stuck_publishing or (f.status == "publish_failed" and f.updated_at >= since):
                summary.stuck.append(f)
            if f.status != "published":
                continue
            (summary.opened if f.created_at >= since else summary.waiting).append(f)
        active_runs(repo, deps, now)
        for r in deps.store.list_runs(repo.name, since - STALE_LOOKBACK):
            if r.finished_at is None or r.finished_at < since:
                continue
            if r.status in ("skipped_backpressure", "skipped_budget"):
                summary.skipped.append(r)
            elif r.status in FAILED_STATUSES:
                summary.failed.append(r)
    notifier = deps.notifier()
    if notifier and (summary.opened or summary.skipped or summary.failed or summary.stuck):
        notifier.send(summary)
    deps.store.put_meta(LAST_SUMMARY_KEY, now.isoformat())
    return summary
