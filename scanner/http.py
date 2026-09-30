"""Shared HTTP client: per-source rate limiting, retries with backoff.

Every outbound request goes through `HttpClient.get_json`, which
  * waits so each rate-limit bucket stays under its requests-per-minute cap,
  * retries network errors, HTTP 429 and 5xx with exponential backoff,
  * honours a Retry-After header when the server sends one,
  * returns None (after logging) instead of raising when a request finally fails,
so one flaky API never crashes the scan loop.
"""
from __future__ import annotations

import logging
import random
import re
import threading
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = "crypto-momentum-scanner/0.1 (personal, non-commercial)"

# Secrets that can appear inside URLs (Telegram bot token, Discord webhook
# token). They're masked before anything is written to the console or log.
_SECRET_PATTERNS = [
    (re.compile(r"/bot\d+:[A-Za-z0-9_-]+"), "/bot<hidden>"),
    (re.compile(r"/webhooks/\d+/[A-Za-z0-9_-]+"), "/webhooks/<hidden>"),
]


def redact(text: str) -> str:
    for pattern, repl in _SECRET_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    """Masks secrets in any log record, whoever wrote it."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


# httpx logs every request URL at INFO; those URLs can carry the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class RateLimiter:
    """Evenly spaces requests so no more than `per_minute` happen per minute."""

    def __init__(self, per_minute: int):
        self.interval = 60.0 / max(1, per_minute)
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self.interval
        if delay > 0:
            time.sleep(delay)

    def penalize(self, seconds: float) -> None:
        """Push the next allowed slot back (after a 429)."""
        with self._lock:
            self._next_allowed = max(self._next_allowed, time.monotonic() + seconds)


class HttpClient:
    def __init__(self, timeout: float = 20.0, max_retries: int = 4,
                 transport: httpx.BaseTransport | None = None):
        self.max_retries = max_retries
        self._limiters: dict[str, RateLimiter] = {}
        self._client = httpx.Client(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            follow_redirects=True,
            transport=transport,
        )

    def add_bucket(self, name: str, per_minute: int) -> None:
        self._limiters[name] = RateLimiter(per_minute)

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str, *, bucket: str, params: dict | None = None,
                 headers: dict | None = None) -> Any | None:
        return self._request("GET", url, bucket=bucket, params=params, headers=headers)[1]

    def get_json_status(self, url: str, *, bucket: str, params: dict | None = None,
                        headers: dict | None = None, expected: tuple[int, ...] = ()
                        ) -> tuple[int | None, Any | None]:
        """Like get_json, but also returns the HTTP status (None = network failure).
        Statuses in `expected` are normal answers ("not found") and are only
        logged quietly; their JSON body is returned too."""
        return self._request("GET", url, bucket=bucket, params=params, headers=headers,
                             expected=expected)

    def post_json_status(self, url: str, body: Any, *, bucket: str, headers: dict | None = None,
                         expected: tuple[int, ...] = ()) -> tuple[int | None, Any | None]:
        return self._request("POST", url, bucket=bucket, json=body, headers=headers,
                             expected=expected)

    def post_json(self, url: str, body: Any, *, bucket: str,
                  headers: dict | None = None) -> Any | None:
        return self._request("POST", url, bucket=bucket, json=body, headers=headers)[1]

    def _request(self, method: str, url: str, *, bucket: str, params: dict | None = None,
                 headers: dict | None = None, json: Any = None,
                 expected: tuple[int, ...] = ()) -> tuple[int | None, Any | None]:
        limiter = self._limiters.get(bucket)
        attempt = 0
        while True:
            if limiter:
                limiter.wait()
            try:
                resp = self._client.request(method, url, params=params, headers=headers, json=json)
            except httpx.HTTPError as exc:
                if attempt >= self.max_retries:
                    log.warning("Giving up on %s after %d tries: %s", redact(url), attempt + 1, redact(str(exc)))
                    return None, None
                delay = _backoff(attempt)
                log.info("Network error on %s (%s); retrying in %.1fs", redact(url), redact(str(exc)), delay)
                time.sleep(delay)
                attempt += 1
                continue

            if 200 <= resp.status_code < 300:
                if resp.status_code == 204 or not resp.content:
                    return resp.status_code, None
                try:
                    return resp.status_code, resp.json()
                except ValueError:
                    log.warning("Non-JSON response from %s", redact(url))
                    return resp.status_code, None

            if resp.status_code in expected:
                log.info("HTTP %s from %s: %s", resp.status_code, redact(url),
                         resp.text[:200].replace("\n", " "))
                try:
                    return resp.status_code, resp.json()
                except ValueError:
                    return resp.status_code, None

            retryable = resp.status_code == 429 or resp.status_code >= 500
            if not retryable or attempt >= self.max_retries:
                log.warning("HTTP %s from %s (giving up): %s", resp.status_code, redact(url),
                            resp.text[:200].replace("\n", " "))
                return resp.status_code, None

            delay = _retry_after(resp) or _backoff(attempt)
            if resp.status_code == 429:
                log.info("Rate-limited by %s; backing off %.1fs", redact(url), delay)
                if limiter:
                    limiter.penalize(delay)
            else:
                log.info("HTTP %s from %s; retrying in %.1fs", resp.status_code, redact(url), delay)
            time.sleep(delay)
            attempt += 1


def _backoff(attempt: int) -> float:
    # 2s, 4s, 8s, 16s ... capped at 60s, with a little jitter.
    return min(60.0, 2.0 * (2 ** attempt)) + random.uniform(0, 0.5)


def _retry_after(resp: httpx.Response) -> float | None:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return min(120.0, max(0.0, float(value)))
    except ValueError:
        return None
