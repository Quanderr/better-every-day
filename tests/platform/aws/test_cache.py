import io
import json

import boto3
import pytest
from moto import mock_aws

from bed.core.models import CacheGrant
from bed.platform.aws.cache import S3CacheStore, StsCacheGrants, cache_key


class Pipe:
    def __init__(self, data: bytes):
        self._inner = io.BytesIO(data)

    def read(self, size: int = -1) -> bytes:
        return self._inner.read(size)


@pytest.fixture
def aws():
    with mock_aws():
        yield


@pytest.fixture
def store(aws):
    boto3.client("s3").create_bucket(Bucket="cache",
                                     CreateBucketConfiguration={"LocationConstraint": "eu-central-1"})
    return S3CacheStore(CacheGrant(bucket="cache", key=cache_key("acme/web"), access_key_id="testing",
                                   secret_access_key="testing", session_token="testing"))


def test_cache_key_is_per_repo_and_path_safe():
    assert cache_key("acme/web") != cache_key("acme-web")
    assert cache_key("acme/web").startswith("repos/") and cache_key("acme/web").count("/") == 2
    assert ".." not in cache_key("../../etc")


def test_grant_scopes_credentials_to_the_repo_object(aws, monkeypatch):
    grants = StsCacheGrants("cache", "arn:aws:iam::123456789012:role/bed-cache")
    calls = []
    original = grants.client.assume_role
    monkeypatch.setattr(grants.client, "assume_role", lambda **kw: calls.append(kw) or original(**kw))
    grant = grants.grant("acme/web")
    assert grant.bucket == "cache" and grant.key == cache_key("acme/web")
    assert grant.access_key_id and grant.secret_access_key and grant.session_token
    statement = json.loads(calls[0]["Policy"])["Statement"]
    assert [s["Resource"] for s in statement] == [f"arn:aws:s3:::cache/{cache_key('acme/web')}"]
    assert "s3:ListBucket" not in calls[0]["Policy"]
    assert calls[0]["DurationSeconds"] == 3600


def test_missing_object_is_a_miss(store):
    assert store.open() is None


def test_upload_streams_unseekable_source_and_round_trips(store):
    store.upload(Pipe(b"archive-bytes"), {"fingerprint": "fp", "created": "1"})
    metadata, body = store.open()
    assert metadata == {"fingerprint": "fp", "created": "1"}
    assert body.read() == b"archive-bytes"


def test_failing_source_leaves_no_object(store):
    class Broken:
        def read(self, size: int = -1) -> bytes:
            raise RuntimeError("archive creation failed")

    with pytest.raises(RuntimeError):
        store.upload(Broken(), {"fingerprint": "fp"})
    assert store.open() is None


def test_delete_removes_object(store):
    store.upload(Pipe(b"x"), {"fingerprint": "fp"})
    store.delete()
    assert store.open() is None


def test_denied_read_is_a_logged_miss(store, monkeypatch, caplog):
    from botocore.exceptions import ClientError

    def denied(**kw):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "no"}}, "GetObject")

    monkeypatch.setattr(store.client, "get_object", denied)
    with caplog.at_level("INFO"):
        assert store.open() is None
    assert "AccessDenied" in caplog.text
