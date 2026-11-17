import json

import boto3


class SecretsManagerStore:
    def __init__(self) -> None:
        self.client = boto3.client("secretsmanager")
        self._cache: dict[str, dict[str, str]] = {}

    def get(self, name: str) -> dict[str, str]:
        if name not in self._cache:
            raw = self.client.get_secret_value(SecretId=name)["SecretString"]
            self._cache[name] = {k: str(v) for k, v in json.loads(raw).items()}
        return dict(self._cache[name])
