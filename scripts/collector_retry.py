#!/usr/bin/env python3
"""Bounded transport retry helper for the Market Data collector.

This module owns no provider selection or research semantics.  It retries only
network transport failures under the caller-provided collector access policy.
HTTP status failures are accounted but never retried here; application-level
provider errors are left to the provider adapter.
"""
from __future__ import annotations

import time
import urllib.error
from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

T = TypeVar("T")


class RetryPolicyError(ValueError):
    pass


def transient_retry_policy(access: Mapping[str, Any]) -> tuple[int, list[float]]:
    policy = ((access.get("http") or {}).get("transient_retry") or {})
    attempts = int(policy.get("max_attempts") or 1)
    if attempts < 1 or attempts > 5:
        raise RetryPolicyError("transient retry max_attempts must be between 1 and 5")
    raw_backoff = policy.get("backoff_seconds") or []
    if not isinstance(raw_backoff, Sequence) or isinstance(raw_backoff, (str, bytes)):
        raise RetryPolicyError("transient retry backoff_seconds must be an array")
    backoff = [float(value) for value in raw_backoff]
    if any(value < 0 or value > 30 for value in backoff):
        raise RetryPolicyError("transient retry backoff seconds must be between 0 and 30")
    if attempts > 1 and len(backoff) < attempts - 1:
        raise RetryPolicyError("transient retry backoff_seconds must cover every retry")
    return attempts, backoff


def call_with_transport_retry(
    call: Callable[[], T],
    *,
    access: Mapping[str, Any],
    on_failed_transport_attempt: Callable[[], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> T:
    """Execute ``call`` with bounded retry for TimeoutError/URLError only.

    ``urllib.error.HTTPError`` is deliberately not retried.  When an accounting
    callback is supplied it is invoked once for every failed network/HTTP
    attempt so a metered provider can conservatively charge that attempt.
    Runtime/application errors are not retried and are not double-accounted.
    """
    attempts, backoff = transient_retry_policy(access)
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except urllib.error.HTTPError:
            if on_failed_transport_attempt is not None:
                on_failed_transport_attempt()
            raise
        except (TimeoutError, urllib.error.URLError):
            if on_failed_transport_attempt is not None:
                on_failed_transport_attempt()
            if attempt >= attempts:
                raise
            delay = backoff[attempt - 1]
            if delay:
                sleep_fn(delay)
    raise AssertionError("unreachable retry state")
