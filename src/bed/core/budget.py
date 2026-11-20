import json
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal

from bed.core.ports import StateStore

USD_QUANTUM = Decimal("0.00000001")


def utc_day(now: datetime) -> str:
    return now.astimezone(UTC).date().isoformat()


class DailyBudget:
    def __init__(self, store: StateStore, repo: str, day: str):
        self.store = store
        self.key = f"budget#daily#{repo}#{day}"

    def reserve(self, run_id: str, cap: float) -> float:
        def claim(stored: str | None) -> str | None:
            held = json.loads(stored or "{}")
            if run_id in held:
                return None
            remaining = Decimal(str(cap)) - sum(Decimal(str(v)) for v in held.values())
            amount = remaining.quantize(USD_QUANTUM, rounding=ROUND_DOWN)
            if amount <= 0:
                return None
            return json.dumps({**held, run_id: float(amount)})

        return float(json.loads(self.store.update_meta(self.key, claim) or "{}").get(run_id, 0))

    def settle(self, run_id: str, cost: float) -> None:
        self.store.update_meta(self.key, lambda stored: json.dumps({**json.loads(stored or "{}"), run_id: cost}))
