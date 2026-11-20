import boto3
import pytest
from helpers import make_candidate
from moto import mock_aws

from bed.core.models import Checkpoint, ExplorationMemory, FileChange, FixResult, WorkerResult
from bed.platform.aws.eventbus import SqsSink, parse_result


def result(content="x"):
    fix = FixResult(candidate=make_candidate(), fingerprint="fp", status="fixed",
                    changes=[FileChange(path="a.py", content=content)], verified=True)
    return WorkerResult(run_id="r", repo="acme/web", base_sha="abc", status="ok", results=[fix], tokens_used=5)


@pytest.fixture
def queue():
    with mock_aws():
        yield boto3.client("sqs").create_queue(QueueName="results")["QueueUrl"]


def test_round_trip(queue):
    SqsSink(queue).emit(result())
    msg = boto3.client("sqs").receive_message(QueueUrl=queue)["Messages"][0]
    assert parse_result(msg["Body"]) == result()


def test_checkpoint_round_trip(queue):
    checkpoint = Checkpoint(run_id="r", repo="acme/web", base_sha="a" * 40, nonce="n",
                            exploration=ExplorationMemory(generation="r", base_sha="a" * 40))
    SqsSink(queue).emit(checkpoint)
    msg = boto3.client("sqs").receive_message(QueueUrl=queue)["Messages"][0]
    assert parse_result(msg["Body"]) == checkpoint


def test_oversize_raises(queue):
    with pytest.raises(ValueError, match="too large"):
        SqsSink(queue).emit(result("x" * 250_000))


@mock_aws
def test_fifo_results_share_one_group_and_deduplicate_retries():
    client = boto3.client("sqs")
    queue = client.create_queue(QueueName="results.fifo", Attributes={
        "FifoQueue": "true", "ContentBasedDeduplication": "true",
    })["QueueUrl"]
    sink = SqsSink(queue)
    first = result()
    second = first.model_copy(update={"run_id": "another-run"})
    sink.emit(first)
    sink.emit(first)
    sink.emit(second)
    messages = client.receive_message(QueueUrl=queue, MaxNumberOfMessages=10,
        MessageSystemAttributeNames=["MessageGroupId", "MessageDeduplicationId"])["Messages"]
    assert len(messages) == 2
    assert {m["Attributes"]["MessageGroupId"] for m in messages} == {"results"}
    assert {parse_result(m["Body"]).run_id for m in messages} == {first.run_id, second.run_id}
