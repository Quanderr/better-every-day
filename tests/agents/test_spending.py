from decimal import Decimal
from types import SimpleNamespace

import pytest

from bed.agents.spending import SpendingClient, SpendLimitExceeded
from bed.core.models import TokenPrices

PRICES = TokenPrices(input=4.4, output=22, cache_read=0.22, cache_write=5.5)
REQUEST = {"messages": [{"role": "user", "content": [{"text": "hello"}]}],
           "inferenceConfig": {"maxTokens": 128}}


def test_accounts_for_cache_separately():
    raw = SimpleNamespace(converse=lambda **kw: {"usage": {"inputTokens": 100, "outputTokens": 10,
                                                           "cacheReadInputTokens": 1000}})
    client = SpendingClient(raw, 1, PRICES)
    client.converse(**REQUEST)
    assert client.spent == Decimal("0.00088")


def test_refuses_call_before_inference_if_full_reservation_does_not_fit():
    calls = []
    client = SpendingClient(SimpleNamespace(converse=lambda **kw: calls.append(kw)), 0.001, PRICES)
    with pytest.raises(SpendLimitExceeded, match="next bounded"):
        client.converse(**REQUEST)
    assert calls == [] and client.spent == 0


def test_stream_usage_releases_unused_reservation():
    raw = SimpleNamespace(converse_stream=lambda **kw: {"stream": iter([
        {"metadata": {"usage": {"inputTokens": 100, "outputTokens": 10}}}])})
    client = SpendingClient(raw, 1, PRICES)
    list(client.converse_stream(**REQUEST)["stream"])
    assert client.spent == Decimal("0.00066")


def test_missing_usage_retains_reservation_and_blocks_more_calls():
    calls = []

    def stream(**kw):
        calls.append(kw)
        return {"stream": iter([])}

    client = SpendingClient(SimpleNamespace(converse_stream=stream), 0.02, PRICES)
    list(client.converse_stream(**REQUEST)["stream"])
    assert client.spent > 0
    with pytest.raises(SpendLimitExceeded):
        client.converse_stream(**REQUEST)
    assert len(calls) == 1


def test_requires_explicit_output_bound():
    client = SpendingClient(None, 1, PRICES)
    with pytest.raises(SpendLimitExceeded, match="output token limit"):
        client.converse(messages=[])


def test_unlimited_still_meters_without_dollar_cap():
    raw = SimpleNamespace(converse=lambda **kw: {"usage": {"outputTokens": 1000000}})
    client = SpendingClient(raw, None, PRICES)
    client.converse(**REQUEST)
    assert client.spent == 22 and not client.blocked


def test_midnight_blocks_old_allowance_before_model_call():
    from datetime import UTC, datetime
    calls = []
    now = [datetime(2026, 11, 20, 23, 59, tzinfo=UTC)]
    raw = SimpleNamespace(converse=lambda **kw: calls.append(kw) or {"usage": {"outputTokens": 1}})
    client = SpendingClient(raw, 15, PRICES, budget_day="2026-11-20", clock=lambda: now[0])
    client.converse(**REQUEST)
    now[0] = datetime(2026, 11, 21, tzinfo=UTC)
    with pytest.raises(SpendLimitExceeded, match="UTC daily budget expired"):
        client.converse(**REQUEST)
    assert len(calls) == 1 and client.blocked
