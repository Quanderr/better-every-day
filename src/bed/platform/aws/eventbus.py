import hashlib

import boto3
from pydantic import TypeAdapter

from bed.core.models import Checkpoint, WorkerMessage, WorkerResult

MAX_BODY_BYTES = 240 * 1024
_MESSAGE = TypeAdapter(WorkerMessage)


class SqsSink:
    def __init__(self, queue_url: str):
        self.queue_url = queue_url
        self.client = boto3.client("sqs")

    def emit(self, message: WorkerResult | Checkpoint) -> None:
        body = message.model_dump_json()
        if len(body.encode()) > MAX_BODY_BYTES:
            raise ValueError(f"result too large for SQS: {len(body.encode())} bytes")
        options = {}
        if self.queue_url.endswith(".fifo"):
            options = {
                "MessageGroupId": "results",
                "MessageDeduplicationId": hashlib.sha256(body.encode()).hexdigest(),
            }
        self.client.send_message(QueueUrl=self.queue_url, MessageBody=body, **options)


def parse_result(record_body: str) -> WorkerResult | Checkpoint:
    return _MESSAGE.validate_json(record_body)
