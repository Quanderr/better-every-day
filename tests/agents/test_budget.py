from types import SimpleNamespace

import pytest

from bed.agents.runner import BudgetExceeded, BudgetGuard


def event(tokens):
    metrics = SimpleNamespace(accumulated_usage={"totalTokens": tokens})
    return SimpleNamespace(agent=SimpleNamespace(event_loop_metrics=metrics))


def test_guard_allows_within_budget():
    BudgetGuard(spent_before=100, budget=1000, deadline=10.0, clock=lambda: 5.0).check(event(800))


def test_guard_stops_when_tokens_exhausted_mid_call():
    with pytest.raises(BudgetExceeded, match="token"):
        BudgetGuard(spent_before=500, budget=1000, deadline=10.0, clock=lambda: 5.0).check(event(500))


def test_guard_stops_after_deadline():
    with pytest.raises(BudgetExceeded, match="time"):
        BudgetGuard(spent_before=0, budget=1000, deadline=10.0, clock=lambda: 10.0).check(event(0))


def test_guard_registers_before_model_call():
    from strands.hooks import BeforeModelCallEvent
    registered = []
    registry = SimpleNamespace(add_callback=lambda ev, cb: registered.append(ev))
    BudgetGuard(spent_before=0, budget=1, deadline=1.0).register_hooks(registry)
    assert registered == [BeforeModelCallEvent]
