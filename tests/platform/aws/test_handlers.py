import pytest
from helpers import example_config, make_finding

from bed.core.control.start_run import start_run
from bed.core.models import WorkerResult
from bed.platform.aws.handlers import result_handler, schedule_handler
from bed.testing.fakes import fake_deps


@pytest.fixture
def deps():
    return fake_deps(example_config())


def test_run_action_starts_session(deps):
    out = schedule_handler({"action": "run", "repo": "acme/web"}, None, deps=deps)
    assert out == {"status": "started"} and len(deps.runtime.started) == 1


def test_summary_action_sends(deps):
    from datetime import timedelta
    deps.store.put_finding(make_finding("fp", created_at=deps.clock() - timedelta(hours=1)))
    out = schedule_handler({"action": "summary"}, None, deps=deps)
    assert out["status"] == "sent" and len(deps.fake_notifier.sent) == 1


def test_summary_action_empty(deps):
    assert schedule_handler({"action": "summary"}, None, deps=deps)["status"] == "empty"


def test_unknown_action_raises(deps):
    with pytest.raises(ValueError):
        schedule_handler({"action": "dance"}, None, deps=deps)


def test_result_batch_reports_only_bad_record(deps):
    run = start_run("acme/web", deps)
    good = WorkerResult(run_id=run.run_id, repo="acme/web", base_sha="a" * 40, status="ok",
                        nonce=run.nonce).model_dump_json()
    event = {"Records": [{"messageId": "m1", "body": good}, {"messageId": "m2", "body": "{not json"}]}
    out = result_handler(event, None, deps=deps)
    assert out == {"batchItemFailures": [{"itemIdentifier": "m2"}]}
    assert deps.store.get_run("acme/web", run.run_id).status == "ok"
