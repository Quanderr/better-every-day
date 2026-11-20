"""Meter every Bedrock call, including SDK structured-output calls and retries."""
import json
from datetime import UTC, datetime
from decimal import Decimal

from bed.core.models import TokenPrices


class SpendLimitExceeded(RuntimeError):
    pass


class SpendingClient:
    def __init__(self, client, limit_usd: float | None, prices: TokenPrices, max_calls: int = 60,
                 budget_day: str | None = None, clock=lambda: datetime.now(UTC)):
        self.raw = client
        self.limit = Decimal(str(limit_usd)) if limit_usd is not None else None
        self.budget_day = budget_day
        self.clock = clock
        self.prices = prices
        self.spent = Decimal(0)
        self.calls = 0
        self.max_calls = max_calls
        self.blocked = False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def _reserve(self, request):
        if self.budget_day is not None and self.clock().astimezone(UTC).date().isoformat() != self.budget_day:
            self.blocked = True
            raise SpendLimitExceeded("UTC daily budget expired; wait for the next run")
        if self.calls >= self.max_calls:
            self.blocked = True
            raise SpendLimitExceeded("model call limit reached")
        # Worker requests contain text/tool JSON only. UTF-8 byte length plus
        # framing allowance deliberately overestimates their input token count.
        # Do not use a token/4 estimate here: that can under-reserve real charges.
        body = json.dumps(request, ensure_ascii=False)
        if '"image"' in body or '"video"' in body or '"document"' in body:
            raise SpendLimitExceeded("non-text requests require separate cost accounting")
        maximum_input = len(body.encode()) + 2048
        maximum_output = request.get("inferenceConfig", {}).get("maxTokens")
        if not maximum_output:
            raise SpendLimitExceeded("an explicit output token limit is required")
        worst_input_rate = max(self.prices.input, self.prices.cache_write)
        reserve = (Decimal(maximum_input) * Decimal(str(worst_input_rate)) +
                   Decimal(maximum_output) * Decimal(str(self.prices.output))) / 1_000_000
        if self.limit is not None and self.spent + reserve > self.limit:
            self.blocked = True
            raise SpendLimitExceeded("dollar limit: insufficient allowance for the next bounded model call")
        self.spent += reserve
        self.calls += 1
        return reserve

    def _settle(self, reserve, usage):
        # If a stream fails without usage metadata, keep its full reservation.
        if usage is None:
            return
        cost = sum(Decimal(str(usage.get(key, 0))) * Decimal(str(rate)) for key, rate in (
            ("inputTokens", self.prices.input), ("outputTokens", self.prices.output),
            ("cacheReadInputTokens", self.prices.cache_read), ("cacheWriteInputTokens", self.prices.cache_write),
        )) / 1_000_000
        self.spent += cost - reserve
        if self.limit is not None and self.spent > self.limit:
            raise SpendLimitExceeded("reported usage exceeded the dollar allowance")

    def converse(self, **request):
        reserve = self._reserve(request)
        response = self.raw.converse(**request)
        self._settle(reserve, response.get("usage"))
        return response

    def converse_stream(self, **request):
        reserve = self._reserve(request)
        response = self.raw.converse_stream(**request)
        stream = response["stream"]

        def events():
            settled = False
            try:
                for event in stream:
                    usage = event.get("metadata", {}).get("usage")
                    if usage is not None and not settled:
                        self._settle(reserve, usage)
                        settled = True
                    yield event
            finally:
                if hasattr(stream, "close"):
                    stream.close()

        return {**response, "stream": events()}
