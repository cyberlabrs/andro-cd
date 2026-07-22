"""Tiny in-memory sliding-window rate limiter (per-process).

Good enough for a single-replica control plane: protects the OAuth flow and
the webhook endpoint from brute force / abuse without external dependencies.
"""
import threading
import time

_lock = threading.Lock()
_hits: dict[str, list[float]] = {}


def allow(key: str, limit: int, window_seconds: float = 60.0) -> bool:
    """Returns True if the caller identified by `key` is within `limit` calls per window."""
    now = time.monotonic()
    with _lock:
        bucket = [t for t in _hits.get(key, []) if now - t < window_seconds]
        if len(bucket) >= limit:
            _hits[key] = bucket
            return False
        bucket.append(now)
        _hits[key] = bucket
        # opportunistic cleanup so the dict doesn't grow unbounded
        if len(_hits) > 10_000:
            for k in [k for k, v in _hits.items() if not v or now - v[-1] > window_seconds]:
                _hits.pop(k, None)
        return True


def client_ip(request) -> str:
    """Best-effort client identifier for rate limiting."""
    return request.client.host if request.client else "unknown"
