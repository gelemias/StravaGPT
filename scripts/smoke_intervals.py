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

from app.intervals.client import (  # noqa: E402
    IntervalsClient,
    format_pace,
    inspect_step_targets,
)
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


def build_open_spec(target_date: date, external_id: str) -> WorkoutSpec:
    """Same shape, but with a warm-up and cool-down left open for the lap button."""
    return WorkoutSpec(
        date=target_date,
        sport="Run",
        name="[smoke test] open warmup - delete me",
        target_type="pace",
        external_id=external_id,
        steps=[
            {"type": "warmup", "target": "easy"},
            {
                "type": "interval",
                "repeat": 2,
                "work": {"distance": "400m", "target": "threshold"},
                "recovery": {"duration": "90s", "target": "easy"},
            },
            {"type": "cooldown", "target": "easy"},
        ],
    )


async def run(target_date: date, keep: bool) -> int:
    settings = get_intervals_settings()
    client = IntervalsClient(settings)
    stamp = target_date.isoformat()
    external_id = f"smoke-test-{stamp}"
    open_external_id = f"smoke-test-open-{stamp}"
    day = target_date.isoformat()

    print(f"Intervals.icu smoke test  athlete={settings.athlete_id}  date={day}")
    print(f"open step style: {settings.open_step_style}\n")

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

    # 2. Push both workouts: a fully timed one and one with open warm-up/cool-down.
    print("\n2. Pushing two workouts (one timed, one with open warm-up/cool-down)")
    specs = {
        external_id: build_spec(target_date, external_id),
        open_external_id: build_open_spec(target_date, open_external_id),
    }
    events = []
    sent_descriptions = {}
    for spec in specs.values():
        rendered = render_workout(
            spec,
            threshold_pace_seconds_per_km=threshold,
            open_step_style=settings.open_step_style,
            open_step_nominal_seconds=settings.open_step_nominal_seconds,
        )
        sent_descriptions[spec.external_id] = rendered.description
        print(f"\n   {spec.external_id}  (open steps: {rendered.open_steps})")
        log_info(f"target={rendered.target} moving_time={rendered.moving_time}s")
        for line in rendered.description.splitlines():
            log_info(f"| {line}")
        for warning in rendered.warnings:
            log_info(f"warning: {warning}")
        events.append(
            build_event(spec, rendered.description, rendered.moving_time, rendered.target)
        )

    try:
        await client.bulk_upsert_events(events)
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    log_ok("\nupsert accepted for both workouts")

    # 3. Verify both are on the calendar, and read back what Intervals.icu stored.
    print("\n3. Verifying the workouts are on the calendar")
    try:
        found = await client.list_events(oldest=day, newest=day, category="WORKOUT")
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1

    by_external_id = {item.get("external_id"): item for item in found}
    for wanted in specs:
        event = by_external_id.get(wanted)
        if event is None:
            log_fail(
                f"workout {wanted} not found on {day}. "
                f"Events that day: {sorted(str(key) for key in by_external_id)}"
            )
            return 1
        log_ok(f"found {wanted} (event id={event.get('id')})")

    # The open-step syntax is the part that cannot be verified offline, so show
    # exactly what came back and whether Intervals.icu kept it intact.
    print("\n   What Intervals.icu stored for the open workout:")
    stored = by_external_id[open_external_id].get("description") or ""
    for line in stored.splitlines():
        log_info(f"| {line}")
    if stored.strip() == sent_descriptions[open_external_id].strip():
        log_ok("stored description matches what was sent - open syntax accepted verbatim")
    else:
        log_info(
            "stored description differs from what was sent. Intervals.icu rewrote it; "
            "open the workout in the UI to see how the step was interpreted."
        )
    log_info(
        "Confirm on the watch: the warm-up should wait for the lap button. "
        "If it arrives with a fixed time instead, set INTERVALS_OPEN_STEP_STYLE=nominal "
        "and report what the UI shows."
    )

    # 3b. The structured targets: pace, not power, and a computed training load.
    print("\n3b. Checking the resolved step targets and training load")
    failures = 0
    try:
        resolved = await client.list_events(
            oldest=day, newest=day, category="WORKOUT", resolve=True
        )
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1

    for event in resolved:
        if event.get("external_id") not in specs:
            continue
        report = inspect_step_targets(event)
        name = event.get("external_id")

        if not report["has_workout_doc"]:
            log_fail(f"{name}: no structured workout_doc came back")
            failures += 1
        elif report["mentions_power"]:
            log_fail(
                f"{name}: a step resolved to POWER. Every percentage must carry an "
                "explicit 'Pace' qualifier, or Intervals.icu reads it as % of FTP."
            )
            failures += 1
        elif not report["mentions_pace"]:
            log_fail(f"{name}: no pace target in the resolved steps")
            failures += 1
        else:
            log_ok(f"{name}: steps resolved with pace targets ({report['step_count']} steps)")

        load = report["training_load"]
        if load and load > 0:
            log_ok(f"{name}: training load {load} ({', '.join(report['load_fields'])})")
        else:
            log_fail(
                f"{name}: no training load computed. Check that Sport Settings has a "
                f"threshold pace for Run. Load-like fields seen: {report['load_fields'] or 'none'}"
            )
            failures += 1

    if failures:
        log_info(
            "Structured targets are wrong. The description text is what Intervals.icu "
            "parses, so inspect it above rather than hand-building workout_doc steps."
        )
        return 1

    if keep:
        print(f"\n--keep given: leaving {sorted(specs)} on the calendar. Delete them yourself.")
        return 0

    # 4. Delete them.
    print("\n4. Deleting the workouts")
    try:
        await client.bulk_delete_events([{"external_id": key} for key in specs])
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    log_ok("delete accepted")

    # 5. Verify they are gone.
    print("\n5. Verifying the workouts are gone")
    try:
        remaining = await client.list_events(oldest=day, newest=day, category="WORKOUT")
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1
    leftovers = [
        item.get("external_id") for item in remaining if item.get("external_id") in specs
    ]
    if leftovers:
        log_fail(f"still on the calendar: {leftovers}. Delete them manually.")
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
