import io
import json

import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from helpers import example_config

from bed.core.models import WorkerInput
from bed.platform.aws.runtime import AgentCoreRuntime

ARN = "arn:aws:bedrock-agentcore:eu-central-1:123456789012:runtime/bed-worker"
SID = "bed-acme-web-20261117T050000Z-aaaaaaaa"


def payload():
    cfg = example_config()
    return WorkerInput(run_id="r", repo=cfg.repo("acme/web"), clone_url="u", read_token="t", model=cfg.model)


def response(body):
    raw = json.dumps(body).encode()
    return {"statusCode": 200, "contentType": "application/json", "runtimeSessionId": SID,
            "response": StreamingBody(io.BytesIO(raw), len(raw))}


def test_invoke_sends_expected_params():
    rt = AgentCoreRuntime(ARN)
    p = payload()
    with Stubber(rt.client) as stub:
        stub.add_response("invoke_agent_runtime", response({"status": "accepted", "run_id": "r"}),
                          {"agentRuntimeArn": ARN, "runtimeSessionId": SID, "payload": p.model_dump_json().encode()})
        rt.start(SID, p)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("body", [{"status": "rejected", "error": "bad"}, {}])
def test_not_accepted_raises(body):
    rt = AgentCoreRuntime(ARN)
    with Stubber(rt.client) as stub, pytest.raises(RuntimeError, match="did not accept"):
        stub.add_response("invoke_agent_runtime", response(body))
        rt.start(SID, payload())


def test_busy_same_session_treated_as_accepted():
    rt = AgentCoreRuntime(ARN)
    with Stubber(rt.client) as stub:
        stub.add_response("invoke_agent_runtime", response({"status": "busy"}))
        rt.start(SID, payload())


def test_client_tolerates_cold_start_without_retry():
    rt = AgentCoreRuntime(ARN)
    assert rt.client.meta.config.read_timeout >= 120
    assert rt.client.meta.config.retries["total_max_attempts"] == 1


def test_read_timeout_is_not_a_failure(monkeypatch):
    from botocore.exceptions import ReadTimeoutError
    rt = AgentCoreRuntime(ARN)

    def slow(**kw):
        raise ReadTimeoutError(endpoint_url="https://x")

    monkeypatch.setattr(rt.client, "invoke_agent_runtime", slow)
    rt.start(SID, payload())
