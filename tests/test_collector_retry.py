from __future__ import annotations

import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from collector_retry import RetryPolicyError, call_with_transport_retry, transient_retry_policy


class CollectorRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.access = {
            "http": {
                "transient_retry": {
                    "max_attempts": 3,
                    "backoff_seconds": [1, 3],
                }
            }
        }

    def test_retries_transient_transport_then_succeeds(self) -> None:
        calls = []
        accounted = []
        sleeps = []

        def operation():
            calls.append(1)
            if len(calls) < 3:
                raise TimeoutError("temporary")
            return "ok"

        result = call_with_transport_retry(
            operation,
            access=self.access,
            on_failed_transport_attempt=lambda: accounted.append(1),
            sleep_fn=sleeps.append,
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(accounted), 2)
        self.assertEqual(sleeps, [1.0, 3.0])

    def test_http_error_is_accounted_but_not_retried(self) -> None:
        calls = []
        accounted = []

        def operation():
            calls.append(1)
            raise urllib.error.HTTPError("https://example.invalid", 503, "unavailable", {}, None)

        with self.assertRaises(urllib.error.HTTPError):
            call_with_transport_retry(
                operation,
                access=self.access,
                on_failed_transport_attempt=lambda: accounted.append(1),
                sleep_fn=lambda _seconds: self.fail("HTTPError must not sleep/retry"),
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(accounted), 1)

    def test_application_error_is_not_retried_or_accounted(self) -> None:
        calls = []
        accounted = []

        def operation():
            calls.append(1)
            raise RuntimeError("provider application error")

        with self.assertRaises(RuntimeError):
            call_with_transport_retry(
                operation,
                access=self.access,
                on_failed_transport_attempt=lambda: accounted.append(1),
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(accounted, [])

    def test_policy_is_bounded(self) -> None:
        attempts, backoff = transient_retry_policy(self.access)
        self.assertEqual(attempts, 3)
        self.assertEqual(backoff, [1.0, 3.0])
        with self.assertRaises(RetryPolicyError):
            transient_retry_policy({"http": {"transient_retry": {"max_attempts": 6, "backoff_seconds": [1] * 5}}})


if __name__ == "__main__":
    unittest.main()
