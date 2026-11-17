import re

import pytest
from helpers import example_config, make_candidate, make_finding

from bed.core.control.publish import (
    branch_name,
    commit_message,
    handle_result,
    pr_body,
    publish_one,
    ticket_body,
)
from bed.core.control.start_run import start_run
from bed.core.fingerprint import fingerprint
from bed.core.models import FileChange, FixResult, WorkerResult
from bed.core.ports import BotPr, TicketRef
from bed.testing.fakes import fake_deps

BASE = "a" * 40


def fp_of(**cand):
    return fingerprint(make_candidate(**{"files": ["src/a.py"], **cand}))


FP = fp_of()


def fixed(fp=None, path="src/a.py", lines=3, **cand):
    content = "".join(f"line{i}\n" for i in range(lines))
    candidate = make_candidate(files=[path], **cand)
    return FixResult(candidate=candidate, fingerprint=fp or fingerprint(candidate), status="fixed",
                     changes=[FileChange(path=path, content=content)], summary="Guard None user.",
                     verify_log="ok", verified=True)


def started(repo="acme/web"):
    deps = fake_deps(example_config())
    run = start_run(repo, deps)
    return deps, run


def ok_result(run, results, **kw):
    return WorkerResult(run_id=run.run_id, repo=run.repo, base_sha=BASE, status="ok", results=results,
                        nonce=run.nonce, **kw)


def test_happy_path_order():
    deps, run = started()
    out = handle_result(ok_result(run, [fixed()], tokens_used=42), deps)
    assert deps.fake_tracker.created[0].project == "WEB"
    pushed = deps.fake_vcs.pushed[0]
    assert pushed.branch.startswith("WEB-1-")
    assert pushed.message.startswith("[WEB-1] fix: ")
    assert pushed.base_sha == BASE
    opened = deps.fake_vcs.opened[0]
    assert opened.labels == ["better-every-day"] and opened.base_branch == "main"
    assert "https://tracker.test/WEB-1" in opened.body
    assert deps.fake_tracker.links[0].ticket_id == "WEB-1"
    f = deps.store.get_finding("acme/web", FP)
    assert f.status == "published" and f.pr_number == 1 and f.ticket_id == "WEB-1"
    assert out.status == "ok" and out.tokens_used == 42 and out.finished_at is not None


def test_duplicate_delivery_publishes_once():
    deps, run = started()
    r = ok_result(run, [fixed()])
    handle_result(r, deps)
    handle_result(r, deps)
    assert len(deps.fake_tracker.created) == 1 and len(deps.fake_vcs.opened) == 1


def test_unknown_run_ignored():
    deps, run = started()
    r = ok_result(run, [fixed()]).model_copy(update={"run_id": "nope"})
    handle_result(r, deps)
    assert deps.fake_tracker.created == []


def test_retry_after_pr_failure_reuses_ticket():
    deps, run = started()
    repo = deps.config.repo("acme/web")
    deps.fake_vcs.fail_next("open_pr")
    with pytest.raises(RuntimeError):
        publish_one(fixed(), BASE, repo, run.run_id, deps)
    assert deps.store.get_finding("acme/web", FP).ticket_id == "WEB-1"
    publish_one(fixed(), BASE, repo, run.run_id, deps)
    assert len(deps.fake_tracker.created) == 1 and len(deps.fake_vcs.pushed) == 1
    assert len(deps.fake_vcs.opened) == 1


def test_publish_failure_isolated_and_resumed_by_next_run():
    deps, run = started()
    deps.fake_vcs.fail_next("open_pr")
    out = handle_result(ok_result(run, [fixed(), fixed(path="src/b.py")], lessons="L"), deps)
    assert out.status == "ok" and deps.store.get_lessons("acme/web") == "L"
    failed = deps.store.get_finding("acme/web", FP)
    assert failed.status == "publish_failed" and "injected failure" in failed.reason
    assert deps.store.get_finding("acme/web", fp_of(files=["src/b.py"])).status == "published"
    deps.fake_vcs.bot_prs["acme/web"] = []
    deps.clock.now = deps.clock.now.replace(hour=9)
    run2 = start_run("acme/web", deps)
    assert FP not in deps.runtime.started[-1][1].known_fingerprints
    handle_result(ok_result(run2, [fixed()]), deps)
    f = deps.store.get_finding("acme/web", FP)
    assert f.status == "published" and f.ticket_id == failed.ticket_id
    assert len(deps.fake_tracker.created) == 2 and len(deps.fake_vcs.opened) == 2


def test_gate_rejection_recorded_not_published():
    deps, run = started()
    handle_result(ok_result(run, [fixed(lines=151)]), deps)
    finding = deps.store.get_finding("acme/web", FP)
    assert finding.status == "rejected_by_gate" and finding.reason == "too_many_lines"
    assert deps.fake_tracker.created == []


def test_gate_uses_base_contents():
    deps, run = started()
    deps.fake_vcs.files[("acme/web", BASE, "src/a.py")] = "".join(f"line{i}\n" for i in range(200))
    res = fixed(lines=201)
    handle_result(ok_result(run, [res]), deps)
    assert deps.store.get_finding("acme/web", FP).status == "published"


def test_known_fingerprint_rejected_as_duplicate():
    deps, run = started()
    deps.store.put_finding(make_finding(FP, status="closed", run_id="older"))
    handle_result(ok_result(run, [fixed()]), deps)
    finding = deps.store.get_finding("acme/web", FP)
    assert finding.status == "closed" and finding.run_id == "older"
    assert deps.fake_tracker.created == []


def test_open_pr_files_lock_paths():
    deps, run = started()
    deps.fake_vcs.bot_prs["acme/web"] = [BotPr(number=9, url="u", state="open", branch="b", files=["src/a.py"])]
    handle_result(ok_result(run, [fixed()]), deps)
    assert deps.store.get_finding("acme/web", FP).reason == "overlaps_open_pr"


def test_two_fixes_same_file_second_rejected():
    deps, run = started()
    handle_result(ok_result(run, [fixed(), fixed(symbol="g")]), deps)
    assert deps.store.get_finding("acme/web", fp_of(symbol="g")).reason == "overlaps_open_pr"


def test_not_fixed_recorded_fix_failed():
    deps, run = started()
    res = fixed().model_copy(update={"status": "fix_failed", "verified": False})
    handle_result(ok_result(run, [res]), deps)
    assert deps.store.get_finding("acme/web", FP).status == "fix_failed"


def test_cap_per_run():
    deps, run = started()
    results = [fixed(path=f"src/{i}.py") for i in range(3)]
    handle_result(ok_result(run, results), deps)
    assert len(deps.fake_vcs.opened) == 2
    assert deps.store.get_finding("acme/web", fp_of(files=["src/2.py"])) is None


def test_zero_results_run_ok():
    deps, run = started()
    out = handle_result(ok_result(run, []), deps)
    assert out.status == "ok" and deps.fake_tracker.created == []


def test_failed_worker_status_propagates():
    deps, run = started()
    r = WorkerResult(run_id=run.run_id, repo="acme/web", status="baseline_red", error="tests red", nonce=run.nonce)
    out = handle_result(r, deps)
    assert out.status == "baseline_red" and out.error == "tests red"
    assert deps.store.get_run("acme/web", run.run_id).status == "baseline_red"


def test_lessons_stored():
    deps, run = started()
    handle_result(ok_result(run, [], lessons="- small PRs merge"), deps)
    assert deps.store.get_lessons("acme/web") == "- small PRs merge"


def test_lessons_none_keeps_existing():
    deps, run = started()
    deps.store.put_lessons("acme/web", "keep")
    handle_result(ok_result(run, []), deps)
    assert deps.store.get_lessons("acme/web") == "keep"


def test_ci_mode_pr_body_says_not_run_locally():
    repo = example_config().repo("acme/api")
    body = pr_body(fixed(), TicketRef(id="API-1", url="https://t/API-1"), repo)
    assert "Not run locally; validated by CI" in body and "https://t/API-1" in body


def test_sandbox_pr_body_lists_commands():
    repo = example_config().repo("acme/web")
    body = pr_body(fixed(), TicketRef(id="WEB-1", url="https://t/WEB-1"), repo)
    assert "pnpm test" in body and "passed" in body


def test_commit_subject_max_72():
    msg = commit_message("WEB-123", make_candidate(title="Very " * 40), "summary")
    subject, _, body = msg.partition("\n\n")
    assert len(subject) <= 72 and subject.startswith("[WEB-123] fix: very")
    assert body == "summary"


def test_commit_type_by_category():
    assert commit_message("W-1", make_candidate(category="test-gap", title="Cover x"), "s").startswith(
        "[W-1] test: cover x")


def test_branch_name():
    name = branch_name("WEB-1", "Handle missing user in Logout!! flow, and also many more words")
    assert name.startswith("WEB-1-handle-missing-user")
    assert len(name) <= len("WEB-1-") + 40 and not name.endswith("-")


def test_earlier_fix_failed_finding_can_be_published():
    deps, run = started()
    deps.store.put_finding(make_finding(FP, status="fix_failed", run_id="older"))
    handle_result(ok_result(run, [fixed()]), deps)
    f = deps.store.get_finding("acme/web", FP)
    assert f.status == "published" and f.run_id == run.run_id


def test_earlier_rejected_finding_rerecorded_for_this_run():
    deps, run = started()
    deps.store.put_finding(make_finding(FP, status="rejected_by_gate", run_id="older"))
    handle_result(ok_result(run, [fixed(lines=151)]), deps)
    f = deps.store.get_finding("acme/web", FP)
    assert f.run_id == run.run_id and f.reason == "too_many_lines"


def test_label_retried_by_next_run_after_failure():
    deps, run = started()
    deps.fake_vcs.fail_next("add_labels")
    handle_result(ok_result(run, [fixed()]), deps)
    assert deps.store.get_finding("acme/web", FP).status == "publish_failed"
    deps.clock.now = deps.clock.now.replace(hour=9)
    run2 = start_run("acme/web", deps)
    handle_result(ok_result(run2, [fixed()]), deps)
    assert deps.fake_vcs.labels[("acme/web", 1)] == {"better-every-day"}
    assert len(deps.fake_vcs.opened) == 1


def test_unsafe_path_never_read_from_vcs():
    deps, run = started()
    res = fixed().model_copy(update={"changes": [FileChange(path="../../../user", content="x")]})
    handle_result(ok_result(run, [res]), deps)
    assert deps.fake_vcs.reads == []
    assert deps.store.get_finding("acme/web", FP).reason == "unsafe_path"


def test_late_result_for_timed_out_run_is_published():
    deps, run = started()
    deps.store.put_run(deps.store.get_run("acme/web", run.run_id).model_copy(update={"status": "timeout"}))
    out = handle_result(ok_result(run, [fixed()]), deps)
    assert out.status == "ok" and len(deps.fake_vcs.opened) == 1


def test_worker_fingerprint_is_recomputed():
    deps, run = started()
    deps.store.put_finding(make_finding(FP, status="closed", run_id="older"))
    handle_result(ok_result(run, [fixed(fp="forged0000000000")]), deps)
    assert deps.fake_tracker.created == [] and deps.store.get_finding("acme/web", "forged0000000000") is None


@pytest.mark.parametrize("sha", ["abc123", "HEAD", "../" + "a" * 37])
def test_malformed_base_sha_fails_run(sha):
    deps, run = started()
    out = handle_result(ok_result(run, [fixed()]).model_copy(update={"base_sha": sha}), deps)
    assert out.status == "failed" and "base_sha" in out.error and deps.fake_tracker.created == []


def test_base_sha_not_on_base_branch_fails_run():
    deps, run = started()
    deps.fake_vcs.foreign_shas.add(BASE)
    out = handle_result(ok_result(run, [fixed()]), deps)
    assert out.status == "failed" and "base_sha" in out.error and deps.fake_vcs.pushed == []
    assert deps.fake_vcs.ancestor_checks == [("acme/web", BASE, "main")]


def test_duplicate_fingerprint_in_one_result_does_not_overwrite_published():
    deps, run = started()
    dup = fixed().model_copy(update={"status": "fix_failed"})
    handle_result(ok_result(run, [fixed(), dup]), deps)
    f = deps.store.get_finding("acme/web", FP)
    assert f.status == "published" and f.pr_number == 1


def test_resume_with_different_changes_is_not_pushed():
    deps, run = started()
    repo = deps.config.repo("acme/web")
    deps.fake_vcs.fail_next("push_branch")
    with pytest.raises(RuntimeError):
        publish_one(fixed(), BASE, repo, run.run_id, deps)
    tampered = fixed().model_copy(update={"changes": [FileChange(path="src/a.py", content="evil\n")]})
    handle_result(ok_result(run, [tampered]), deps)
    assert deps.fake_vcs.pushed == []
    assert deps.store.get_finding("acme/web", FP).status == "publish_failed"


def test_resume_with_same_changes_completes():
    deps, run = started()
    repo = deps.config.repo("acme/web")
    deps.fake_vcs.fail_next("push_branch")
    with pytest.raises(RuntimeError):
        publish_one(fixed(), BASE, repo, run.run_id, deps)
    handle_result(ok_result(run, [fixed()]), deps)
    assert len(deps.fake_vcs.pushed) == 1 and deps.store.get_finding("acme/web", FP).status == "published"


def test_retry_with_different_changes_pushes_fresh_branch():
    deps, run = started()
    deps.fake_vcs.fail_next("open_pr")
    handle_result(ok_result(run, [fixed()]), deps)
    old_branch = deps.fake_vcs.pushed[0].branch
    deps.clock.now = deps.clock.now.replace(hour=9)
    run2 = start_run("acme/web", deps)
    handle_result(ok_result(run2, [fixed(lines=5)]), deps)
    assert len(deps.fake_vcs.pushed) == 2
    new = deps.fake_vcs.pushed[1]
    assert new.branch != old_branch and new.branch.startswith(old_branch)
    assert deps.fake_vcs.opened[0].branch == new.branch
    f = deps.store.get_finding("acme/web", FP)
    assert f.status == "published" and f.branch == new.branch and f.ticket_id == "WEB-1"
    assert len(deps.fake_tracker.created) == 1


def test_retry_with_same_changes_reuses_branch():
    deps, run = started()
    deps.fake_vcs.fail_next("open_pr")
    handle_result(ok_result(run, [fixed()]), deps)
    deps.clock.now = deps.clock.now.replace(hour=9)
    run2 = start_run("acme/web", deps)
    handle_result(ok_result(run2, [fixed()]), deps)
    assert len(deps.fake_vcs.pushed) == 1 and deps.fake_vcs.opened[0].branch == deps.fake_vcs.pushed[0].branch


def test_known_fingerprint_checked_before_reading_files():
    deps, run = started()
    deps.store.put_finding(make_finding(FP, status="merged", run_id="older", pr_number=3))
    deps.fake_vcs.fail_next("read_file")
    handle_result(ok_result(run, [fixed()]), deps)
    f = deps.store.get_finding("acme/web", FP)
    assert f.status == "merged" and f.pr_number == 3 and deps.fake_vcs.reads == []


def test_mark_publish_failed_never_overwrites_blocking_finding():
    from bed.core.control.publish import _mark_publish_failed
    deps, run = started()
    deps.store.put_finding(make_finding(FP, status="closed", run_id="older"))
    _mark_publish_failed(fixed(), deps.config.repo("acme/web"), run.run_id, "boom", deps)
    assert deps.store.get_finding("acme/web", FP).status == "closed"


def test_bodies_neutralise_mentions_images_and_html():
    nasty = "ping @acme/security ![x](https://evil.test/p.png) <img src=https://evil.test/i>"
    res = fixed(evidence=nasty, rationale=nasty).model_copy(update={"summary": nasty})
    repo = example_config().repo("acme/web")
    for body in (pr_body(res, TicketRef(id="WEB-1", url="https://t/WEB-1"), repo), ticket_body(res)):
        outside = re.sub(r"```text\n.*?\n```", "", body, flags=re.DOTALL)
        assert "@acme" not in outside and "![" not in outside and "<img" not in outside
        assert body.count("```") == 2


def test_result_with_wrong_nonce_ignored():
    deps, run = started()
    out = handle_result(ok_result(run, [fixed()]).model_copy(update={"nonce": "0" * 32}), deps)
    assert out.status == "started" and deps.fake_tracker.created == []
    assert deps.store.get_run("acme/web", run.run_id).status == "started"


def test_late_result_older_than_a_day_ignored():
    from datetime import timedelta
    deps, run = started()
    deps.store.put_run(deps.store.get_run("acme/web", run.run_id).model_copy(update={"status": "timeout"}))
    deps.clock.now = deps.clock.now + timedelta(hours=25)
    out = handle_result(ok_result(run, [fixed()]), deps)
    assert out.status == "timeout" and deps.fake_vcs.opened == []


def test_lessons_capped():
    deps, run = started()
    handle_result(ok_result(run, [], lessons="\n".join(f"- line {i} " + "x" * 300 for i in range(50))), deps)
    stored = deps.store.get_lessons("acme/web")
    assert len(stored) <= 2000 and len(stored.splitlines()) <= 10


def test_read_token_revoked_when_result_arrives():
    deps, run = started()
    handle_result(ok_result(run, []), deps)
    assert deps.fake_vcs.revoked == [("acme/web", "tok-1")]


def test_pr_body_breaks_gitlab_quick_actions_and_closing_keywords():
    nasty = "fine\n/target_branch release\n  /assign me\nCloses #12 and fixes acme/web#3"
    res = fixed(rationale=nasty).model_copy(update={"summary": nasty})
    body = pr_body(res, TicketRef(id="W", url="u"), example_config().repo("acme/web"))
    outside = re.sub(r"```text\n.*?\n```", "", body, flags=re.DOTALL)
    assert not re.search(r"(?m)^\s*/", outside)
    assert not re.search(r"(?i)\b(closes|fixes)\s+\S*#\d", outside)


def test_commit_message_sanitised():
    cand = make_candidate(title="Fix\nCo-authored-by: x <x@y> [skip ci]")
    msg = commit_message("WEB-1", cand, "Guard None.\n[ci skip]\nSigned-off-by: evil <e@v>\nskip-checks: true\nCloses #4")
    subject, _, body = msg.partition("\n\n")
    assert "\n" not in subject and "skip ci" not in subject.lower()
    lowered = msg.lower()
    assert "[skip ci]" not in lowered and "[ci skip]" not in lowered and "skip-checks" not in lowered
    assert "signed-off-by" not in lowered and not re.search(r"(?i)closes\s+#4", body)
    assert body.startswith("Guard None.")
