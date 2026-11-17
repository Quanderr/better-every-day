import boto3
import pytest
from helpers import make_candidate
from moto import mock_aws

from bed.core.models import FileChange, FixResult, WorkerResult
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


def test_oversize_raises(queue):
    with pytest.raises(ValueError, match="too large"):
        SqsSink(queue).emit(result("x" * 250_000))
