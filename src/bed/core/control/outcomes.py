import logging

from bed.core.control.deps import ControlDeps
from bed.core.models import BOT_LABEL, Finding, RepoConfig
from bed.core.ports import BotPr

log = logging.getLogger(__name__)


def sync_outcomes(repo: RepoConfig, deps: ControlDeps) -> list[BotPr]:
    vcs = deps.vcs(repo)
    prs = vcs.list_bot_prs(repo.name)
    by_number = {pr.number: pr for pr in prs if pr.state != "open"}
    for finding in deps.store.list_findings(repo.name):
        if finding.status == "published" and finding.pr_number in by_number:
            _settle(finding, by_number[finding.pr_number], repo, deps)
        elif finding.status == "publish_failed" and finding.pr_number is not None:
            _reconcile(finding, repo, deps)
    return prs


def _settle(finding: Finding, pr: BotPr, repo: RepoConfig, deps: ControlDeps) -> None:
    comment = deps.vcs(repo).close_comment(repo.name, pr.number) if pr.state == "closed" else None
    deps.store.put_finding(finding.model_copy(update={
        "status": pr.state, "close_comment": comment, "updated_at": deps.clock(),
    }))


def _reconcile(finding: Finding, repo: RepoConfig, deps: ControlDeps) -> None:
    vcs = deps.vcs(repo)
    try:
        pr = vcs.get_pr(repo.name, finding.pr_number)
        if pr.state != "open":
            _settle(finding, pr, repo, deps)
            return
        vcs.add_labels(repo.name, pr.number, [BOT_LABEL])
        if finding.ticket_id:
            deps.tracker(repo).link_pr(finding.ticket_id, finding.pr_url or pr.url, finding.title)
    except Exception:
        log.exception("could not reconcile finding %s", finding.fingerprint)
        return
    deps.store.put_finding(finding.model_copy(update={"status": "published", "reason": None,
                                                      "updated_at": deps.clock()}))
