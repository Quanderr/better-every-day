import time
from collections.abc import Callable
from typing import Any

import httpx

RETRIES = 3
RETRY_AFTER_CAP_S = 60
IDEMPOTENT = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    try:
        return min(float(value), RETRY_AFTER_CAP_S)
    except ValueError:
        return None


class HttpApi:
    def __init__(self, base_url: str, headers: dict[str, str], error: Callable[[int, str], Exception],
                 client: httpx.Client | None = None, auth: httpx.Auth | None = None):
        self.base_url = base_url.rstrip("/")
        self.headers = headers
        self.error = error
        self.client = client or httpx.Client(timeout=30, auth=auth)
        self.sleep = time.sleep

    def _retryable(self, method: str, status: int) -> bool:
        if status == 429:
            return True
        return status >= 500 and method.upper() in IDEMPOTENT

    def send(self, method: str, path: str, *, ok_status: tuple[int, ...] = (),
             headers: dict[str, str] | None = None, **kwargs: Any) -> httpx.Response:
        url = path if path.startswith("http") else f"{self.base_url}{path}"
        merged = {**self.headers, **(headers or {})}
        for attempt in range(RETRIES + 1):
            response = self.client.request(method, url, headers=merged, **kwargs)
            if self._retryable(method, response.status_code) and attempt < RETRIES:
                delay = _retry_after(response)
                self.sleep(delay if delay is not None else 2 ** attempt)
                continue
            break
        if response.status_code in ok_status:
            return response
        if response.status_code >= 400:
            raise self.error(response.status_code, response.text)
        return response

    def paginate(self, path: str, params: dict[str, Any],
                 headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        response = self.send("GET", path, params={**params, "per_page": 100}, headers=headers)
        while True:
            items.extend(response.json())
            next_url = response.links.get("next", {}).get("url")
            if not next_url:
                return items
            response = self.send("GET", next_url, headers=headers)
