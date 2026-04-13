from __future__ import annotations

import time
from collections import defaultdict

from fastapi import Request


class SlidingWindowLimiter:
    """Limiteur simple en mémoire (adapté à un worker uvicorn)."""

    def __init__(self, max_hits: int, window_seconds: int) -> None:
        self.max_hits = max_hits
        self.window = float(window_seconds)
        self._hits: dict[str, list[float]] = defaultdict(list)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        bucket = self._hits[key]
        cutoff = now - self.window
        bucket[:] = [t for t in bucket if t > cutoff]
        if len(bucket) >= self.max_hits:
            return False
        bucket.append(now)
        return True


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    if request.client:
        return request.client.host or "unknown"
    return "unknown"
