import hashlib

import boto3

from bed.core.models import WorkerResult

MAX_BODY_BYTES = 240 * 1024


class SqsSink:
    def __init__(self, queue_url: str):
        self.queue_url = queue_url
        self.client = boto3.client("sqs")

    def emit(self, result: WorkerResult) -> None:
        body = result.model_dump_json()
        if len(body.encode()) > MAX_BODY_BYTES:
            raise ValueError(f"result too large for SQS: {len(body.encode())} bytes")
        options = {}
        if self.queue_url.endswith(".fifo"):
            options = {
                "MessageGroupId": "results",
                "MessageDeduplicationId": hashlib.sha256(body.encode()).hexdigest(),
            }
        self.client.send_message(QueueUrl=self.queue_url, MessageBody=body, **options)


def parse_result(record_body: str) -> WorkerResult:
    return WorkerResult.model_validate_json(record_body)
