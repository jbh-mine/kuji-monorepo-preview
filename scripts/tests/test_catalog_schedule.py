"""Offline regressions for bounded, timezone-safe GitHub catalog publication."""
import contextlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "catalog_schedule.py"
SPEC = importlib.util.spec_from_file_location("catalog_schedule", SCRIPT)
schedule = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = schedule
SPEC.loader.exec_module(schedule)

TARGET_TEXT = "2026-10-22T10:10:00+09:00"
TARGET = datetime(2026, 10, 22, 10, 10, tzinfo=schedule.KST)
TOKEN = "test-secret-actions-token-never-log-this"
ENV = {"GITHUB_TOKEN": TOKEN, "GITHUB_REPOSITORY": schedule.PUBLIC_REPOSITORY}


class FakeClock:
    def __init__(self, now):
        self.value = now
        self.sleeps = []

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.value += timedelta(seconds=seconds)


class Response:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None


class FakeOpener:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome)


def http_error(code):
    return urllib.error.HTTPError("https://api.github.com/", code,
                                  "private upstream text " + TOKEN, {}, None)


class CatalogScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "sync-status.json").write_text(json.dumps({
            "status": "ok", "failed": 0, "translation": {"pending": 0}}), encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def marker(self, **changes):
        value = {"version": 1, "targetAt": TARGET_TEXT,
                 "publishedAt": "2026-10-22T10:11:00+09:00", "healthy": True, "runId": "42"}
        value.update(changes)
        path = self.root / schedule.MARKER
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def plan(self, *, now=None, mode="scheduled", scheduled_for=TARGET_TEXT):
        return schedule.publication_plan(mode=mode, scheduled_for=scheduled_for,
                                         root=self.root, now=now or TARGET + timedelta(minutes=2))

    def test_target_accepts_explicit_utc_equivalent_and_normalizes_korean_time(self):
        self.assertEqual(schedule.parse_target("2026-10-22T01:10:00Z"), TARGET)
        self.assertEqual(schedule.timestamp(schedule.parse_target(TARGET_TEXT)), TARGET_TEXT)

    def test_target_rejects_naive_invalid_non_target_and_fractional_values(self):
        for value in ("2026-10-22T10:10:00", "2026-10-22", "2026-02-30T10:10:00+09:00",
                      "2026-10-22T10:11:00+09:00", "2026-10-22T10:10:00.1+09:00",
                      "2026-10-22T10:10:00+25:00", "2026-10-22T10:10:00+09:00\nsecret", None):
            with self.subTest(value=value), self.assertRaises(schedule.ScheduleError):
                schedule.parse_target(value)

    def test_clock_requires_timezone(self):
        with self.assertRaises(schedule.ScheduleError):
            schedule.daily_target(datetime(2026, 10, 22, 1, 10))

    def test_timer_date_is_korean_day_even_before_utc_midnight(self):
        clock = FakeClock(datetime(2026, 10, 21, 19, 47, tzinfo=timezone.utc))
        dispatched = []
        result = schedule.run_timer(now_fn=clock.now, sleep_fn=clock.sleep, dispatch_fn=dispatched.append)
        self.assertEqual(dispatched, [TARGET])
        self.assertEqual(result["target_at"], TARGET_TEXT)
        self.assertEqual(result["dispatched_at"], "2026-10-22T10:03:00+09:00")
        self.assertEqual(sum(clock.sleeps), 5 * 3600 + 16 * 60)
        self.assertLessEqual(max(clock.sleeps), 30)

    def test_timer_absorbs_five_hour_cron_delay(self):
        clock = FakeClock(TARGET.replace(hour=9, minute=47))
        dispatched = []
        schedule.run_timer(now_fn=clock.now, sleep_fn=clock.sleep, dispatch_fn=dispatched.append)
        self.assertEqual(sum(clock.sleeps), 16 * 60)
        self.assertEqual(dispatched, [TARGET])

    def test_late_timer_dispatches_today_immediately_without_tomorrow_rollover(self):
        clock = FakeClock(TARGET.replace(hour=15, minute=9))
        dispatched = []
        result = schedule.run_timer(now_fn=clock.now, sleep_fn=clock.sleep, dispatch_fn=dispatched.append)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(dispatched, [TARGET])
        self.assertEqual(result["target_at"], TARGET_TEXT)

    def test_timer_rejects_wait_beyond_five_hours_twenty_minutes(self):
        clock = FakeClock(TARGET.replace(hour=4, minute=30))
        dispatched = []
        with self.assertRaises(schedule.ScheduleError):
            schedule.run_timer(now_fn=clock.now, sleep_fn=clock.sleep, dispatch_fn=dispatched.append)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(dispatched, [])

    def test_wait_is_bounded_when_clock_stalls(self):
        sleeps = []
        with self.assertRaises(schedule.ScheduleError):
            schedule.wait_until(TARGET, max_wait_seconds=60, absolute_limit=60,
                                now_fn=lambda: TARGET - timedelta(seconds=60), sleep_fn=sleeps.append)
        self.assertEqual(sleeps, [30, 30])

    def test_wait_is_bounded_when_clock_moves_backwards(self):
        clock = FakeClock(TARGET - timedelta(seconds=60))
        def backwards_sleep(seconds):
            clock.sleeps.append(seconds)
            clock.value -= timedelta(seconds=seconds)
        with self.assertRaises(schedule.ScheduleError):
            schedule.wait_until(TARGET, max_wait_seconds=60, absolute_limit=60,
                                now_fn=clock.now, sleep_fn=backwards_sleep)
        self.assertEqual(sum(clock.sleeps), 60)

    def test_gate_waits_to_target_in_thirty_second_intervals(self):
        clock = FakeClock(TARGET - timedelta(minutes=7))
        waited = schedule.wait_until(TARGET, max_wait_seconds=600, absolute_limit=600,
                                     now_fn=clock.now, sleep_fn=clock.sleep)
        self.assertEqual(waited, 420)
        self.assertEqual(clock.now(), TARGET)
        self.assertLessEqual(max(clock.sleeps), 30)

    def test_gate_does_not_wait_when_late(self):
        sleeps = []
        self.assertEqual(schedule.wait_until(TARGET, max_wait_seconds=600, absolute_limit=600,
                         now_fn=lambda: TARGET + timedelta(hours=4), sleep_fn=sleeps.append), 0)
        self.assertEqual(sleeps, [])

    def test_gate_rejects_unbounded_non_finite_and_negative_waits(self):
        for limit in (601, -1, float("inf"), float("nan")):
            with self.subTest(limit=limit), self.assertRaises(schedule.ScheduleError):
                schedule.wait_until(TARGET, max_wait_seconds=limit, absolute_limit=600,
                                    now_fn=lambda: TARGET)

    def test_plan_exposes_validated_github_outputs(self):
        self.assertEqual(self.plan(), {"should_run": "true", "target_at": TARGET_TEXT,
                         "mode": "scheduled", "prepared_at": "2026-10-22T10:12:00+09:00"})

    def test_plan_accepts_exact_ten_minute_preparation_boundary(self):
        self.assertEqual(self.plan(now=TARGET - timedelta(minutes=10))["should_run"], "true")

    def test_plan_rejects_more_than_ten_minutes_early(self):
        with self.assertRaises(schedule.ScheduleError):
            self.plan(now=TARGET - timedelta(seconds=601))

    def test_plan_rejects_previous_day_and_future_day_targets(self):
        for target in ("2026-10-21T10:10:00+09:00", "2026-10-23T10:10:00+09:00"):
            with self.subTest(target=target), self.assertRaises(schedule.ScheduleError):
                self.plan(scheduled_for=target)

    def test_missing_target_derives_current_korean_day_without_waiting_until_tomorrow(self):
        result = self.plan(now=TARGET + timedelta(hours=8), scheduled_for="")
        self.assertEqual(result["target_at"], TARGET_TEXT)
        self.assertEqual(result["should_run"], "true")

    def test_healthy_completed_same_target_publication_skips_duplicate_schedule(self):
        self.marker()
        self.assertEqual(self.plan()["should_run"], "false")

    def test_unhealthy_publication_never_suppresses_retry(self):
        self.marker(healthy=False)
        self.assertEqual(self.plan()["should_run"], "true")

    def test_a_later_failed_manual_refresh_does_not_hide_behind_a_healthy_daily_marker(self):
        self.marker()
        for health in ({"status": "partial", "failed": 1, "translation": {"pending": 0}},
                       {"status": "ok", "failed": 0, "translation": {"pending": 2}}):
            (self.root / "sync-status.json").write_text(json.dumps(health), encoding="utf-8")
            self.assertEqual(self.plan()["should_run"], "true")

    def test_missing_or_invalid_current_health_never_suppresses_a_recovery(self):
        self.marker()
        path = self.root / "sync-status.json"
        path.unlink()
        self.assertEqual(self.plan()["should_run"], "true")
        path.write_text("not-json", encoding="utf-8")
        self.assertEqual(self.plan()["should_run"], "true")

    def test_string_health_is_not_a_success_marker(self):
        self.marker(healthy="true")
        self.assertEqual(self.plan()["should_run"], "true")

    def test_pre_target_immediate_publication_does_not_suppress_daily_refresh(self):
        self.marker(publishedAt="2026-10-22T09:55:00+09:00")
        self.assertEqual(self.plan()["should_run"], "true")

    def test_future_publication_time_is_not_a_success_marker(self):
        self.marker(publishedAt="2026-10-22T10:15:00+09:00")
        self.assertEqual(self.plan()["should_run"], "true")

    def test_other_day_publication_does_not_suppress_today(self):
        self.marker(targetAt="2026-10-21T10:10:00+09:00", publishedAt="2026-10-21T10:11:00+09:00")
        self.assertEqual(self.plan()["should_run"], "true")

    def test_corrupt_or_non_object_marker_is_retried(self):
        path = self.root / schedule.MARKER
        path.parent.mkdir(exist_ok=True)
        for raw in ("{", "[]", "null", '{"healthy":true}',
                    '{"healthy":true,"targetAt":"not-an-iso","publishedAt":"secret"}'):
            path.write_text(raw, encoding="utf-8")
            with self.subTest(raw=raw):
                self.assertEqual(self.plan()["should_run"], "true")

    def test_immediate_push_always_runs_despite_healthy_daily_marker(self):
        self.marker()
        result = self.plan(mode="immediate", scheduled_for="")
        self.assertEqual(result["should_run"], "true")
        self.assertEqual(result["target_at"], TARGET_TEXT)

    def test_immediate_mode_rejects_a_conflicting_scheduled_target(self):
        with self.assertRaises(schedule.ScheduleError):
            self.plan(mode="immediate")

    def test_record_is_atomic_contains_actual_korean_timestamp_and_run_identifier(self):
        now = datetime(2026, 10, 22, 1, 12, tzinfo=timezone.utc)
        marker = schedule.record_publication(root=self.root, target=TARGET, healthy=True,
                                             now=now, run_id="123456789")
        self.assertEqual(marker, {"version": 1, "targetAt": TARGET_TEXT,
                         "publishedAt": "2026-10-22T10:12:00+09:00", "healthy": True,
                         "runId": "123456789"})
        self.assertEqual(json.loads((self.root / schedule.MARKER).read_text(encoding="utf-8")), marker)
        self.assertEqual(list((self.root / ".github").glob("*.tmp")), [])
        self.assertTrue(schedule.already_published(self.root, TARGET, now))

    def test_unhealthy_record_allows_followup_retry(self):
        self.marker()
        schedule.record_publication(root=self.root, target=TARGET, healthy=False,
                                    now=TARGET + timedelta(minutes=2), run_id="42")
        self.assertEqual(self.plan()["should_run"], "true")

    def test_record_rejects_wrong_day_non_boolean_and_non_numeric_run_id(self):
        cases = [{"now": TARGET + timedelta(days=1), "healthy": True, "run_id": "42"},
                 {"now": TARGET, "healthy": "true", "run_id": "42"},
                 {"now": TARGET, "healthy": True, "run_id": TOKEN}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(schedule.ScheduleError) as caught:
                schedule.record_publication(root=self.root, target=TARGET, **case)
            self.assertNotIn(TOKEN, str(caught.exception))
        self.assertFalse((self.root / schedule.MARKER).exists())

    def test_dispatch_uses_only_fixed_github_endpoint_and_public_scheduling_inputs(self):
        opener = FakeOpener([204])
        schedule.dispatch_publication(TARGET, environ=ENV, opener=opener)
        request, timeout = opener.requests[0]
        self.assertEqual(request.full_url, "https://api.github.com/repos/jbh-mine/kuji-monorepo-preview/actions/workflows/daily-catalog.yml/dispatches")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(timeout, 5)
        self.assertEqual(json.loads(request.data), {"ref": "main", "inputs": {
                         "publication_mode": "scheduled", "scheduled_for": TARGET_TEXT}})
        self.assertNotIn(TOKEN.encode(), request.data)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + TOKEN)

    def test_dispatch_accepts_current_and_legacy_github_success_statuses(self):
        for status in (200, 202, 204):
            with self.subTest(status=status):
                schedule.dispatch_publication(TARGET, environ=ENV, opener=FakeOpener([status]))

    def test_dispatch_never_accepts_other_repositories_or_missing_unsafe_tokens(self):
        cases = [{**ENV, "GITHUB_REPOSITORY": "someone/another-repo"},
                 {**ENV, "GITHUB_TOKEN": ""}, {**ENV, "GITHUB_TOKEN": "secret\nheader"},
                 {**ENV, "GITHUB_TOKEN": "secret token"}]
        for environ in cases:
            opener = FakeOpener([204])
            with self.subTest(environ=environ), self.assertRaises(schedule.ScheduleError) as caught:
                schedule.dispatch_publication(TARGET, environ=environ, opener=opener)
            self.assertEqual(opener.requests, [])
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_dispatch_retries_transient_http_and_network_errors_with_bounded_backoff(self):
        opener = FakeOpener([http_error(429), urllib.error.URLError(TOKEN), 204])
        sleeps = []
        schedule.dispatch_publication(TARGET, environ=ENV, opener=opener, sleep_fn=sleeps.append)
        self.assertEqual(len(opener.requests), 3)
        self.assertEqual(sleeps, [1, 2])
        self.assertEqual([timeout for _, timeout in opener.requests], [5, 5, 5])

    def test_dispatch_does_not_retry_client_auth_or_redirect_errors_or_leak_messages(self):
        for status in (301, 302, 307, 400, 401, 403, 404, 422):
            opener = FakeOpener([http_error(status)])
            sleeps = []
            with self.subTest(status=status), self.assertRaises(schedule.ScheduleError) as caught:
                schedule.dispatch_publication(TARGET, environ=ENV, opener=opener, sleep_fn=sleeps.append)
            self.assertEqual(len(opener.requests), 1)
            self.assertEqual(sleeps, [])
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_dispatch_abandons_three_failed_requests_without_logging_sensitive_error(self):
        for errors in ([http_error(503), http_error(502), http_error(500)],
                       [TimeoutError(TOKEN), urllib.error.URLError(TOKEN), OSError(TOKEN)]):
            opener = FakeOpener(errors)
            sleeps = []
            with self.subTest(errors=errors), self.assertRaises(schedule.ScheduleError) as caught:
                schedule.dispatch_publication(TARGET, environ=ENV, opener=opener, sleep_fn=sleeps.append)
            self.assertEqual(len(opener.requests), 3)
            self.assertEqual(sleeps, [1, 2])
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_http_opener_disables_proxy_use_and_credential_redirects(self):
        opener = schedule.github_opener()
        redirects = [handler for handler in opener.handlers if isinstance(handler, schedule.NoRedirect)]
        self.assertEqual(len(redirects), 1)
        self.assertIsNone(redirects[0].redirect_request(None, None, 302, "", {}, "https://evil.example/"))
        self.assertFalse(any(isinstance(handler, __import__("urllib.request").request.ProxyHandler)
                             and handler.proxies for handler in opener.handlers))

    def test_cli_plan_writes_only_validated_outputs(self):
        destination = self.root / "outputs.txt"
        stdout = io.StringIO()
        with patch.dict(os.environ, {"GITHUB_OUTPUT": str(destination), "GITHUB_TOKEN": TOKEN}), \
             patch.object(schedule, "utc_now", return_value=TARGET + timedelta(minutes=2)), \
             contextlib.redirect_stdout(stdout):
            result = schedule.main(["plan", "--mode", "scheduled", "--scheduled-for", TARGET_TEXT,
                                    "--root", str(self.root)])
        self.assertEqual(result, 0)
        self.assertIn("should_run=true\n", destination.read_text(encoding="utf-8"))
        self.assertIn("target_at=" + TARGET_TEXT, destination.read_text(encoding="utf-8"))
        self.assertNotIn(TOKEN, stdout.getvalue() + destination.read_text(encoding="utf-8"))

    def test_cli_dispatch_failure_redacts_exception_details(self):
        stderr = io.StringIO()
        with patch.object(schedule, "run_timer", side_effect=schedule.ScheduleError("GitHub dispatch failed (HTTP 401).")), \
             patch.dict(os.environ, {"GITHUB_TOKEN": TOKEN}), contextlib.redirect_stderr(stderr):
            result = schedule.main(["timer"])
        self.assertEqual(result, 1)
        self.assertNotIn(TOKEN, stderr.getvalue())
        self.assertIn("HTTP 401", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
