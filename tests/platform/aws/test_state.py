from datetime import timedelta

import boto3
import pytest
from helpers import NOW, make_finding
from moto import mock_aws

from bed.core.models import RunRecord
from bed.platform.aws.state import DynamoStateStore


@pytest.fixture
def store():
    with mock_aws():
        boto3.client("dynamodb").create_table(
            TableName="bed", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"},
                                  {"AttributeName": "sk", "AttributeType": "S"}],
        )
        yield DynamoStateStore("bed")


def run(run_id, started_at):
    return RunRecord(run_id=run_id, repo="acme/web", status="ok", started_at=started_at)


def test_run_round_trip(store):
    r = run("20261117T050000Z-aaaaaaaa", NOW)
    store.put_run(r)
    assert store.get_run("acme/web", r.run_id) == r
    assert store.get_run("acme/web", "missing") is None


def test_list_runs_since(store):
    store.put_run(run("20261115T050000Z-aaaaaaaa", NOW - timedelta(days=2)))
    store.put_run(run("20261117T040000Z-bbbbbbbb", NOW - timedelta(hours=1)))
    store.put_run(run("20261117T050000Z-cccccccc", NOW))
    got = store.list_runs("acme/web", NOW - timedelta(hours=1))
    assert [r.run_id for r in got] == ["20261117T040000Z-bbbbbbbb", "20261117T050000Z-cccccccc"]


def test_finding_round_trip_and_list_only_findings(store):
    f = make_finding("fp1", pr_number=3)
    store.put_finding(f)
    store.put_lessons("acme/web", "L")
    store.put_run(run("20261117T050000Z-aaaaaaaa", NOW))
    assert store.get_finding("acme/web", "fp1") == f
    assert store.get_finding("acme/web", "nope") is None
    assert store.list_findings("acme/web") == [f]
    assert store.list_findings("acme/other") == []


def test_lessons_and_meta(store):
    assert store.get_lessons("acme/web") == ""
    store.put_lessons("acme/web", "L")
    assert store.get_lessons("acme/web") == "L"
    assert store.get_meta("k") is None
    store.put_meta("k", "v")
    assert store.get_meta("k") == "v"


def test_lease_is_exclusive_until_expiry(store):
    assert store.try_lease("start#acme/web", NOW, timedelta(minutes=5))
    assert not store.try_lease("start#acme/web", NOW + timedelta(minutes=1), timedelta(minutes=5))
    assert store.try_lease("start#acme/web", NOW + timedelta(minutes=6), timedelta(minutes=5))


def test_budget_reservation_is_idempotent_and_capped(store):
    assert store.reserve_budget("budget", "first", 3, 3.7) == 3
    assert store.reserve_budget("budget", "first", 3, 3.7) == 3
    assert store.reserve_budget("budget", "second", 3, 3.7) == 0.7
    assert store.reserve_budget("budget", "third", 3, 3.7) == 0


def test_budget_compare_and_swap_handles_an_overlapping_start(store, monkeypatch):
    put = store.table.put_item
    other = DynamoStateStore("bed")
    raced = False

    def racing_put(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert other.reserve_budget("budget", "other", 3, 3.7) == 3
        return put(**kwargs)

    monkeypatch.setattr(store.table, "put_item", racing_put)
    assert store.reserve_budget("budget", "ours", 3, 3.7) == 0.7
    assert other.reserve_budget("budget", "later", 3, 3.7) == 0


def test_settlement_releases_only_unused_allowance_idempotently(store):
    assert store.reserve_budget("daily", "first", 15, 15) == 15
    store.settle_budget("daily", "first", 1.5)
    store.settle_budget("daily", "first", 1.5)
    assert store.reserve_budget("daily", "next", 15, 15) == 13.5


def test_settlement_retries_without_overwriting_concurrent_reservations(store, monkeypatch):
    assert store.reserve_budget("daily", "first", 5, 15) == 5
    other = DynamoStateStore("bed")
    original = store.table.put_item
    raced = False

    def put(**kwargs):
        nonlocal raced
        if not raced:
            raced = True
            assert other.reserve_budget("daily", "other", 5, 15) == 5
        return original(**kwargs)

    monkeypatch.setattr(store.table, "put_item", put)
    store.settle_budget("daily", "first", 1)
    assert other.reserve_budget("daily", "last", 15, 15) == 9
