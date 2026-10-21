"""Bounded GitHub-only timing and publication markers for the public catalog.

The early timer absorbs cron delivery delays. It dispatches the ordinary publisher
near the target time without occupying that publisher's deployment concurrency.
All date decisions use Korea/Japan UTC+09:00, never the runner's local timezone.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import ssl
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

KST = timezone(timedelta(hours=9))
PUBLIC_REPOSITORY = "jbh-mine/kuji-monorepo-preview"
WORKFLOW = "daily-catalog.yml"
MARKER = Path(".github/catalog-publication.json")
TIMER_MAX_WAIT = 5 * 3600 + 20 * 60
GATE_MAX_WAIT = 10 * 60
POLL_SECONDS = 30
ISO_AWARE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})")


class ScheduleError(ValueError):
    """A safe, non-sensitive error suitable for a public Actions log."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ScheduleError("A timezone-aware clock is required.")
    return value.astimezone(KST)


def timestamp(value: datetime) -> str:
    return local_time(value).isoformat(timespec="seconds")


def parse_timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not ISO_AWARE.fullmatch(value):
        raise ScheduleError("Expected an ISO timestamp with an explicit timezone.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ScheduleError("Invalid ISO timestamp.") from None
    return local_time(result)


def parse_target(value: str) -> datetime:
    result = parse_timestamp(value)
    if (result.hour, result.minute, result.second, result.microsecond) != (10, 10, 0, 0):
        raise ScheduleError("The publication target must be 10:10:00 in Korea.")
    return result


def daily_target(now: datetime) -> datetime:
    return local_time(now).replace(hour=10, minute=10, second=0, microsecond=0)


def require_today(target: datetime, now: datetime) -> None:
    if local_time(target).date() != local_time(now).date():
        raise ScheduleError("The target must belong to the current Korean calendar day.")


def wait_until(target: datetime, *, max_wait_seconds: float,
               absolute_limit: float, now_fn: Callable[[], datetime] = utc_now,
               sleep_fn: Callable[[float], None] = time.sleep) -> float:
    """Wait in short intervals; clock stalls/backward jumps cannot extend the cap."""
    if (not math.isfinite(max_wait_seconds) or max_wait_seconds < 0
            or max_wait_seconds > absolute_limit):
        raise ScheduleError("The requested wait exceeds the permitted bound.")
    now = local_time(now_fn())
    remaining = (local_time(target) - now).total_seconds()
    if remaining > max_wait_seconds:
        raise ScheduleError("The target is too far in the future for this operation.")
    slept = 0.0
    while remaining > 0:
        budget = max_wait_seconds - slept
        if budget <= 0:
            raise ScheduleError("The bounded wait expired before the target.")
        interval = min(POLL_SECONDS, remaining, budget)
        sleep_fn(interval)
        slept += interval
        remaining = (local_time(target) - local_time(now_fn())).total_seconds()
    return slept


def already_published(root: Path, target: datetime, now: datetime) -> bool:
    """Only a healthy, actually published same-day marker suppresses a retry."""
    try:
        marker = json.loads((root / MARKER).read_text(encoding="utf-8"))
        if not isinstance(marker, dict) or marker.get("healthy") is not True:
            return False
        marker_target = parse_target(marker.get("targetAt"))
        published = parse_timestamp(marker.get("publishedAt"))
        if not (marker_target == target and published.date() == target.date()
                and target <= published <= local_time(now)):
            return False
        # A later manual refresh can become partial after today's successful
        # scheduled publication. It must remain eligible for the recovery run.
        health = json.loads((root / "sync-status.json").read_text(encoding="utf-8"))
        return (isinstance(health, dict) and health.get("status") == "ok"
                and health.get("failed") == 0
                and isinstance(health.get("translation"), dict)
                and health["translation"].get("pending") == 0)
    except (OSError, ValueError, TypeError):
        return False


def publication_plan(*, mode: str, scheduled_for: str | None, root: Path,
                     now: datetime) -> dict[str, str]:
    if mode not in {"immediate", "scheduled"}:
        raise ScheduleError("Unknown publication mode.")
    if mode == "immediate" and scheduled_for:
        raise ScheduleError("Immediate publication cannot specify a scheduled target.")
    now = local_time(now)
    target = parse_target(scheduled_for) if scheduled_for else daily_target(now)
    require_today(target, now)
    if mode == "scheduled" and (target - now).total_seconds() > GATE_MAX_WAIT:
        raise ScheduleError("Scheduled publication may prepare at most ten minutes early.")
    should_run = mode == "immediate" or not already_published(root, target, now)
    return {"should_run": str(should_run).lower(), "target_at": timestamp(target),
            "mode": mode, "prepared_at": timestamp(now)}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        # Never forward the Actions credential, including to another GitHub URL.
        return None


def github_opener():
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()))


def dispatch_publication(target: datetime, *, environ: dict[str, str] | None = None,
                         opener=None, sleep_fn: Callable[[float], None] = time.sleep) -> None:
    environ = os.environ if environ is None else environ
    if environ.get("GITHUB_REPOSITORY") != PUBLIC_REPOSITORY:
        raise ScheduleError("Dispatch is restricted to the public kuji repository.")
    token = environ.get("GITHUB_TOKEN", "")
    if not token or len(token) > 4096 or any(ord(ch) < 33 or ord(ch) > 126 for ch in token):
        raise ScheduleError("A valid Actions token is required for dispatch.")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{PUBLIC_REPOSITORY}/actions/workflows/{WORKFLOW}/dispatches",
        data=json.dumps({"ref": "main", "inputs": {"publication_mode": "scheduled",
                        "scheduled_for": timestamp(target)}}).encode("utf-8"), method="POST",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "kuji-catalog-timer"})
    opener = github_opener() if opener is None else opener
    for attempt in range(3):
        try:
            with opener.open(request, timeout=5) as response:
                status = response.status
                if status in {200, 202, 204}:
                    return
                # A successful-looking response body is never printed or persisted.
                raise ScheduleError("GitHub dispatch returned an unexpected response.")
        except urllib.error.HTTPError as error:
            status = error.code
            retryable = status == 429 or 500 <= status <= 599
            if not retryable or attempt == 2:
                raise ScheduleError(f"GitHub dispatch failed (HTTP {status}).") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 2:
                raise ScheduleError("GitHub dispatch failed after three bounded attempts.") from None
        if attempt < 2:
            sleep_fn(attempt + 1)


def run_timer(*, now_fn: Callable[[], datetime] = utc_now,
              sleep_fn: Callable[[float], None] = time.sleep,
              dispatch_fn: Callable[[datetime], None] = dispatch_publication,
              max_wait_seconds: float = TIMER_MAX_WAIT) -> dict[str, str]:
    now = local_time(now_fn())
    target = daily_target(now)
    prepare_at = target - timedelta(minutes=7)
    wait_until(prepare_at, max_wait_seconds=max_wait_seconds, absolute_limit=TIMER_MAX_WAIT,
               now_fn=now_fn, sleep_fn=sleep_fn)
    # A delayed cron recovers today's publication immediately; never roll to tomorrow.
    require_today(target, now_fn())
    dispatch_fn(target)
    return {"target_at": timestamp(target), "mode": "scheduled",
            "dispatched_at": timestamp(now_fn())}


def record_publication(*, root: Path, target: datetime, healthy: bool,
                       now: datetime, run_id: str = "") -> dict:
    target = local_time(target)
    # Keep publication records unambiguous even for immediate updates before 10:10.
    parse_target(timestamp(target))
    require_today(target, now)
    if type(healthy) is not bool:
        raise ScheduleError("Publication health must be a boolean.")
    if run_id and not re.fullmatch(r"\d{1,30}", run_id):
        raise ScheduleError("Invalid GitHub run identifier.")
    marker = {"version": 1, "targetAt": timestamp(target), "publishedAt": timestamp(now),
              "healthy": healthy, "runId": run_id}
    destination = root / MARKER
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix="catalog-publication-", suffix=".tmp",
                                         delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(marker, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temporary, destination)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return marker


def output(values: dict[str, str]) -> None:
    destination = os.environ.get("GITHUB_OUTPUT")
    if destination:
        with open(destination, "a", encoding="utf-8") as stream:
            for key, value in values.items():
                if not re.fullmatch(r"[a-z_]+", key) or "\n" in value or "\r" in value:
                    raise ScheduleError("Invalid workflow output.")
                stream.write(f"{key}={value}\n")
    print(json.dumps(values, ensure_ascii=False, sort_keys=True))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    timer = commands.add_parser("timer", help="Wait for 10:03 KST, then dispatch the publisher")
    timer.add_argument("--max-wait-seconds", type=float, default=TIMER_MAX_WAIT)
    plan = commands.add_parser("plan", help="Validate a target and skip successful duplicate schedules")
    plan.add_argument("--mode", choices=("immediate", "scheduled"), required=True)
    plan.add_argument("--scheduled-for", default="")
    plan.add_argument("--root", type=Path, default=Path("."))
    gate = commands.add_parser("gate", help="Wait until the validated same-day 10:10 publication time")
    gate.add_argument("--target", required=True)
    gate.add_argument("--max-wait-seconds", type=float, default=GATE_MAX_WAIT)
    record = commands.add_parser("record", help="Record an actual completed Pages publication")
    record.add_argument("--target", required=True)
    record.add_argument("--healthy", choices=("true", "false"), required=True)
    record.add_argument("--root", type=Path, default=Path("."))
    args = parser.parse_args(argv)
    try:
        if args.command == "timer":
            output(run_timer(max_wait_seconds=args.max_wait_seconds))
        elif args.command == "plan":
            output(publication_plan(mode=args.mode, scheduled_for=args.scheduled_for,
                                    root=args.root, now=utc_now()))
        elif args.command == "gate":
            target = parse_target(args.target)
            require_today(target, utc_now())
            wait_until(target, max_wait_seconds=args.max_wait_seconds, absolute_limit=GATE_MAX_WAIT)
            output({"target_at": timestamp(target), "gate_opened_at": timestamp(utc_now())})
        else:
            marker = record_publication(root=args.root, target=parse_target(args.target),
                                        healthy=args.healthy == "true", now=utc_now(),
                                        run_id=os.environ.get("GITHUB_RUN_ID", ""))
            output({"target_at": marker["targetAt"], "published_at": marker["publishedAt"],
                    "healthy": str(marker["healthy"]).lower()})
        return 0
    except ScheduleError as error:
        print(f"Catalog timing error: {error}", file=sys.stderr)
        return 1
    except OSError:
        print("Catalog timing error: workflow output or publication marker could not be written.",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
