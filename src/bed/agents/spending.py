"""Meter every Bedrock call, including SDK structured-output calls and retries."""
import json
from decimal import Decimal

from bed.core.models import TokenPrices


class SpendLimitExceeded(RuntimeError):
    pass


class SpendingClient:
    def __init__(self, client, limit_usd: float, prices: TokenPrices, max_calls: int = 60):
        self.raw = client
        self.limit = Decimal(str(limit_usd))
        self.prices = prices
        self.spent = Decimal(0)
        self.calls = 0
        self.max_calls = max_calls
        self.blocked = False

    def __getattr__(self, name):
        return getattr(self.raw, name)

    def _reserve(self, request):
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
        if self.spent + reserve > self.limit:
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
        if self.spent > self.limit:
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
