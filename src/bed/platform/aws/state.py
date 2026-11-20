import json
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from bed.core.models import Finding, RunRecord

META_PK = "META"
LEASE_PK = "LEASE"


def _repo_pk(repo: str) -> str:
    return f"REPO#{repo}"


class DynamoStateStore:
    def __init__(self, table_name: str):
        self.table = boto3.resource("dynamodb").Table(table_name)

    def _put(self, pk: str, sk: str, data: str) -> None:
        self.table.put_item(Item={"pk": pk, "sk": sk, "data": data})

    def _get(self, pk: str, sk: str) -> str | None:
        item = self.table.get_item(Key={"pk": pk, "sk": sk}, ConsistentRead=True).get("Item")
        return item["data"] if item else None

    def _query(self, condition: Any) -> list[str]:
        items: list[str] = []
        kwargs: dict[str, Any] = {"KeyConditionExpression": condition, "ConsistentRead": True}
        while True:
            page = self.table.query(**kwargs)
            items.extend(i["data"] for i in page["Items"])
            if "LastEvaluatedKey" not in page:
                return items
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    def put_run(self, run: RunRecord) -> None:
        self._put(_repo_pk(run.repo), f"RUN#{run.run_id}", run.model_dump_json())

    def get_run(self, repo: str, run_id: str) -> RunRecord | None:
        data = self._get(_repo_pk(repo), f"RUN#{run_id}")
        return RunRecord.model_validate_json(data) if data else None

    def list_runs(self, repo: str, since: datetime) -> list[RunRecord]:
        lower = f"RUN#{since.strftime('%Y%m%dT%H%M%SZ')}"
        condition = Key("pk").eq(_repo_pk(repo)) & Key("sk").between(lower, "RUN#~")
        return [RunRecord.model_validate_json(d) for d in self._query(condition)]

    def get_finding(self, repo: str, fingerprint: str) -> Finding | None:
        data = self._get(_repo_pk(repo), f"FP#{fingerprint}")
        return Finding.model_validate_json(data) if data else None

    def put_finding(self, finding: Finding) -> None:
        self._put(_repo_pk(finding.repo), f"FP#{finding.fingerprint}", finding.model_dump_json())

    def list_findings(self, repo: str) -> list[Finding]:
        condition = Key("pk").eq(_repo_pk(repo)) & Key("sk").begins_with("FP#")
        return [Finding.model_validate_json(d) for d in self._query(condition)]

    def get_lessons(self, repo: str) -> str:
        return self._get(_repo_pk(repo), "LESSONS") or ""

    def put_lessons(self, repo: str, text: str) -> None:
        self._put(_repo_pk(repo), "LESSONS", text)

    def get_meta(self, key: str) -> str | None:
        return self._get(META_PK, key)

    def put_meta(self, key: str, value: str) -> None:
        self._put(META_PK, key, value)

    def reserve_budget(self, key: str, run_id: str, requested: float, maximum: float) -> float:
        for _ in range(20):
            old = self.get_meta(key)
            reservations = json.loads(old or "{}")
            if run_id in reservations:
                return float(reservations[run_id])
            remaining = Decimal(str(maximum)) - sum(Decimal(str(v)) for v in reservations.values())
            amount = min(Decimal(str(requested)), remaining).quantize(Decimal("0.00000001"), rounding=ROUND_DOWN)
            if amount <= 0:
                return 0
            reservations[run_id] = float(amount)
            options = {"ConditionExpression": "#data = :old", "ExpressionAttributeValues": {":old": old}} if old else {
                "ConditionExpression": "attribute_not_exists(#data)"}
            try:
                self.table.put_item(Item={"pk": META_PK, "sk": key, "data": json.dumps(reservations)},
                                    ExpressionAttributeNames={"#data": "data"}, **options)
                return float(amount)
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        raise RuntimeError("could not atomically reserve model budget")

    def settle_budget(self, key: str, run_id: str, cost: float) -> None:
        for _ in range(20):
            old = self.get_meta(key)
            reservations = json.loads(old or "{}")
            if run_id not in reservations:
                raise RuntimeError("missing model budget reservation")
            if reservations[run_id] == cost:
                return
            reservations[run_id] = cost
            try:
                self.table.put_item(Item={"pk": META_PK, "sk": key, "data": json.dumps(reservations)},
                                    ConditionExpression="#data = :old",
                                    ExpressionAttributeNames={"#data": "data"},
                                    ExpressionAttributeValues={":old": old})
                return
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
        raise RuntimeError("could not atomically settle model budget")

    def try_lease(self, key: str, now: datetime, ttl: timedelta) -> bool:
        try:
            self.table.put_item(
                Item={"pk": LEASE_PK, "sk": key, "expires_at": int((now + ttl).timestamp())},
                ConditionExpression="attribute_not_exists(pk) OR expires_at < :now",
                ExpressionAttributeValues={":now": int(now.timestamp())},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True
