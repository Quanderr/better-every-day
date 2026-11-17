import json
import logging

import boto3
from botocore.config import Config
from botocore.exceptions import ReadTimeoutError

from bed.core.models import WorkerInput

READ_TIMEOUT_S = 120
ACCEPTED = {"accepted", "busy"}
log = logging.getLogger(__name__)


class AgentCoreRuntime:
    def __init__(self, runtime_arn: str):
        self.runtime_arn = runtime_arn
        self.client = boto3.client(
            "bedrock-agentcore",
            config=Config(retries={"total_max_attempts": 1, "mode": "standard"}, read_timeout=READ_TIMEOUT_S),
        )

    def start(self, session_id: str, payload: WorkerInput) -> None:
        try:
            response = self.client.invoke_agent_runtime(
                agentRuntimeArn=self.runtime_arn,
                runtimeSessionId=session_id,
                payload=payload.model_dump_json().encode(),
            )
        except ReadTimeoutError:
            log.warning("worker start timed out for %s; leaving run started", session_id)
            return
        body = json.loads(response["response"].read() or b"{}")
        if body.get("status") not in ACCEPTED:
            raise RuntimeError(f"worker did not accept run: {body.get('status')} {body.get('error', '')}".strip())
