#!/usr/bin/env python3
"""End-to-end smoke test for the Intervals.icu integration.

Pushes ONE throwaway workout, verifies it landed on the calendar, deletes it and
verifies it is gone. Run this before loading a whole training week.

It talks to the Intervals.icu API directly (not through MCP) so a failure points
at the API chain rather than at the transport.

    python scripts/smoke_intervals.py
    python scripts/smoke_intervals.py --date 2026-07-29 --keep
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.intervals.client import IntervalsClient, format_pace  # noqa: E402
from app.intervals.config import get_intervals_settings  # noqa: E402
from app.intervals.errors import IntervalsError  # noqa: E402
from app.intervals.models import WorkoutSpec, build_event  # noqa: E402
from app.intervals.workout_dsl import render_workout  # noqa: E402


def log_ok(message: str) -> None:
    print(f"  \033[32mOK\033[0m   {message}")


def log_fail(message: str) -> None:
    print(f"  \033[31mFAIL\033[0m {message}")


def log_info(message: str) -> None:
    print(f"       {message}")


def build_spec(target_date: date, external_id: str) -> WorkoutSpec:
    return WorkoutSpec(
        date=target_date,
        sport="Run",
        name="[smoke test] delete me",
        target_type="pace",
        external_id=external_id,
        steps=[
            {"type": "warmup", "duration": "10min", "target": "easy"},
            {
                "type": "interval",
                "repeat": 2,
                "work": {"distance": "400m", "target": "threshold"},
                "recovery": {"duration": "90s", "target": "easy"},
            },
            {"type": "cooldown", "duration": "5min", "target": "easy"},
        ],
    )


async def run(target_date: date, keep: bool) -> int:
    settings = get_intervals_settings()
    client = IntervalsClient(settings)
    external_id = f"smoke-test-{target_date.isoformat()}"
    day = target_date.isoformat()

    print(f"Intervals.icu smoke test  athlete={settings.athlete_id}  date={day}")
    print(f"external_id={external_id}\n")

    # 1. Authentication + sport settings.
    print("1. Reading sport settings (checks authentication)")
    try:
        await client.get_sport_settings()
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    log_ok("authenticated")

    paces, pace_warnings = await client.threshold_paces(["Run"])
    for warning in pace_warnings:
        log_info(f"warning: {warning}")
    threshold = paces.get("Run")
    if threshold:
        log_ok(f"Run threshold pace: {format_pace(threshold)}")

    # 2. Push one workout.
    print("\n2. Pushing one workout")
    spec = build_spec(target_date, external_id)
    rendered = render_workout(spec, threshold_pace_seconds_per_km=threshold)
    for warning in rendered.warnings:
        log_info(f"warning: {warning}")
    log_info(f"target={rendered.target} moving_time={rendered.moving_time}s")
    for line in rendered.description.splitlines():
        log_info(f"| {line}")

    event = build_event(spec, rendered.description, rendered.moving_time, rendered.target)
    try:
        await client.bulk_upsert_events([event])
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    log_ok("upsert accepted")

    # 3. Verify it is on the calendar.
    print("\n3. Verifying the workout is on the calendar")
    try:
        events = await client.list_events(oldest=day, newest=day, category="WORKOUT")
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    matches = [item for item in events if item.get("external_id") == external_id]
    if not matches:
        log_fail(
            f"workout {external_id} not found on {day}. "
            f"Events that day: {[item.get('external_id') for item in events]}"
        )
        return 1
    log_ok(f"found event id={matches[0].get('id')}")

    if keep:
        print(f"\n--keep given: leaving {external_id} on the calendar. Delete it yourself.")
        return 0

    # 4. Delete it.
    print("\n4. Deleting the workout")
    try:
        await client.bulk_delete_events([{"external_id": external_id}])
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    log_ok("delete accepted")

    # 5. Verify it is gone.
    print("\n5. Verifying the workout is gone")
    try:
        events = await client.list_events(oldest=day, newest=day, category="WORKOUT")
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    if any(item.get("external_id") == external_id for item in events):
        log_fail(f"workout {external_id} is still on the calendar. Delete it manually.")
        return 1
    log_ok("calendar is clean")

    print("\nAll steps passed. The push/list/delete chain works.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date",
        default=(date.today() + timedelta(days=1)).isoformat(),
        help="Day to use for the test workout (YYYY-MM-DD). Defaults to tomorrow.",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete the workout, so you can inspect it in the UI.",
    )
    args = parser.parse_args()

    try:
        target_date = date.fromisoformat(args.date)
    except ValueError:
        print(f"--date must be YYYY-MM-DD, got {args.date!r}")
        return 2

    try:
        return asyncio.run(run(target_date, args.keep))
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
