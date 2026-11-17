from helpers import example_config, make_finding

from bed.core.control.outcomes import sync_outcomes
from bed.core.ports import BotPr
from bed.testing.fakes import fake_deps


def setup(state, comment=""):
    deps = fake_deps(example_config())
    deps.store.put_finding(make_finding("fp1", pr_number=7))
    deps.fake_vcs.bot_prs["acme/web"] = [BotPr(number=7, url="u", state=state)]
    deps.fake_vcs.comments[("acme/web", 7)] = comment
    repo = deps.config.repo("acme/web")
    return deps, sync_outcomes(repo, deps)


def test_merged_pr_marks_finding_merged():
    deps, prs = setup("merged")
    assert deps.store.get_finding("acme/web", "fp1").status == "merged"
    assert [p.number for p in prs] == [7]


def test_closed_pr_stores_close_comment():
    deps, _ = setup("closed", "not worth it")
    f = deps.store.get_finding("acme/web", "fp1")
    assert f.status == "closed" and f.close_comment == "not worth it"


def test_open_pr_leaves_finding_published():
    deps, _ = setup("open")
    assert deps.store.get_finding("acme/web", "fp1").status == "published"


def test_already_merged_finding_not_rewritten():
    deps, _ = setup("merged")
    first = deps.store.get_finding("acme/web", "fp1")
    deps.clock.now = deps.clock.now.replace(hour=9)
    sync_outcomes(deps.config.repo("acme/web"), deps)
    assert deps.store.get_finding("acme/web", "fp1").updated_at == first.updated_at


def test_close_comment_fetched_only_on_transition():
    deps2 = fake_deps(example_config())
    deps2.store.put_finding(make_finding("fp1", pr_number=7))
    deps2.fake_vcs.bot_prs["acme/web"] = [BotPr(number=7, url="u", state="closed")]
    deps2.fake_vcs.comments[("acme/web", 7)] = "too noisy"
    sync_outcomes(deps2.config.repo("acme/web"), deps2)
    assert deps2.store.get_finding("acme/web", "fp1").close_comment == "too noisy"
    sync_outcomes(deps2.config.repo("acme/web"), deps2)
    assert deps2.fake_vcs.comment_calls == [("acme/web", 7)]


def reconcile_setup(state):
    deps = fake_deps(example_config())
    deps.store.put_finding(make_finding("fp1", status="publish_failed", pr_number=7, ticket_id="WEB-1",
                                        pr_url="u7"))
    deps.fake_vcs.bot_prs["acme/web"] = []
    deps.fake_vcs.prs_by_number[("acme/web", 7)] = BotPr(number=7, url="u7", state=state)
    deps.fake_vcs.comments[("acme/web", 7)] = "no thanks"
    sync_outcomes(deps.config.repo("acme/web"), deps)
    return deps, deps.store.get_finding("acme/web", "fp1")


def test_publish_failed_with_closed_pr_becomes_closed():
    _, f = reconcile_setup("closed")
    assert f.status == "closed" and f.close_comment == "no thanks"


def test_publish_failed_with_merged_pr_becomes_merged():
    assert reconcile_setup("merged")[1].status == "merged"


def test_publish_failed_with_open_pr_is_completed():
    deps, f = reconcile_setup("open")
    assert f.status == "published"
    assert deps.fake_vcs.labels[("acme/web", 7)] == {"better-every-day"}
    assert deps.fake_tracker.links[0].ticket_id == "WEB-1"


def test_publish_failed_reconcile_error_leaves_finding():
    deps = fake_deps(example_config())
    deps.store.put_finding(make_finding("fp1", status="publish_failed", pr_number=7, ticket_id="WEB-1"))
    deps.fake_vcs.prs_by_number[("acme/web", 7)] = BotPr(number=7, url="u7", state="open")
    deps.fake_vcs.fail_next("add_labels")
    sync_outcomes(deps.config.repo("acme/web"), deps)
    assert deps.store.get_finding("acme/web", "fp1").status == "publish_failed"
