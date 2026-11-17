import json

import boto3
from moto import mock_aws

from bed.platform.aws.secrets import SecretsManagerStore


def test_secrets_parsed_and_cached(monkeypatch):
    with mock_aws():
        boto3.client("secretsmanager").create_secret(Name="bed/x", SecretString=json.dumps({"a": "1"}))
        store = SecretsManagerStore()
        calls = []
        original = store.client.get_secret_value
        monkeypatch.setattr(store.client, "get_secret_value", lambda **kw: calls.append(kw) or original(**kw))
        assert store.get("bed/x") == {"a": "1"}
        assert store.get("bed/x") == {"a": "1"}
        assert len(calls) == 1
