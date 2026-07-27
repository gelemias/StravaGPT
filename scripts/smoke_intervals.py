#!/usr/bin/env python3
"""End-to-end smoke test for the Intervals.icu integration.

Pushes two throwaway workouts, verifies Intervals.icu generated resolved pace
steps and positive training load, deletes them, and verifies they are gone. Run
this before loading a whole training week.

It talks to the Intervals.icu API directly (not through MCP) so a failure points
at the API chain rather than at the transport.

    python scripts/smoke_intervals.py
    python scripts/smoke_intervals.py --date 2026-07-29 --keep
"""

from __future__ import annotations

import argparse
import asyncio
import json
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

    # 1. Authentication + required threshold pace.
    print("1. Reading Run threshold_pace (checks authentication and prerequisites)")
    try:
        paces = await client.require_threshold_paces(["Run"])
    except IntervalsError as exc:
        log_fail(str(exc))
        return 1

    threshold = paces["Run"]
    log_ok(f"authenticated; Run threshold pace: {format_pace(threshold)}")

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

    async def cleanup_after_failure() -> None:
        if keep:
            log_info(f"--keep given: leaving {sorted(specs)} on the calendar")
            return
        try:
            await client.bulk_delete_events([{"external_id": key} for key in specs])
        except IntervalsError as exc:
            log_fail(f"cleanup failed; delete {sorted(specs)} manually: {exc}")
        else:
            log_info("removed the temporary workouts after the failed check")

    # 3. Verify both are on the calendar, and read back what Intervals.icu stored.
    print("\n3. Verifying the workouts are on the calendar")
    try:
        found = await client.list_events(oldest=day, newest=day, category="WORKOUT")
    except IntervalsError as exc:
        log_fail(str(exc))
        await cleanup_after_failure()
        return 1

    by_external_id = {item.get("external_id"): item for item in found}
    for wanted in specs:
        event = by_external_id.get(wanted)
        if event is None:
            log_fail(
                f"workout {wanted} not found on {day}. "
                f"Events that day: {sorted(str(key) for key in by_external_id)}"
            )
            await cleanup_after_failure()
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

    # 3b. GET the pushed events with resolve=true. Intervals.icu must have
    # generated structured steps from description, resolved pace to m/s, and
    # calculated a positive load.
    print("\n3b. GET events?resolve=true: checking resolved pace steps and training load")
    failures = 0
    try:
        resolved_events = await client.list_events(
            oldest=day,
            newest=day,
            category="WORKOUT",
            resolve=True,
        )
    except IntervalsError as exc:
        log_fail(f"GET events?resolve=true failed: {exc}")
        await cleanup_after_failure()
        return 1
    resolved_by_external_id = {
        event.get("external_id"): event for event in resolved_events
    }

    for name in (external_id, open_external_id):
        event = resolved_by_external_id.get(name)
        if event is None:
            log_fail(f"{name}: missing from GET events?resolve=true response")
            failures += 1
            continue

        report = inspect_step_targets(event)

        if not report["has_workout_doc"]:
            log_fail(f"{name}: Intervals.icu did not generate a structured workout_doc")
            failures += 1
        elif not report["step_count"]:
            log_fail(f"{name}: Intervals.icu generated 0 structured steps")
            failures += 1
        elif report["mentions_power"]:
            log_fail(
                f"{name}: a generated step resolved to POWER instead of pace"
            )
            failures += 1
        elif not report["pace_resolved_mps"]:
            log_fail(f"{name}: pace targets were not resolved to m/s")
            log_info(
                "resolved workout_doc: "
                + json.dumps(report["workout_doc"], ensure_ascii=False)[:4000]
            )
            failures += 1
        else:
            log_ok(
                f"{name}: {report['step_count']} generated steps with pace resolved to m/s"
            )

        load = report["training_load"]
        if load and load > 0:
            log_ok(f"{name}: icu_training_load={load}")
        else:
            log_fail(
                f"{name}: icu_training_load must be > 0, got "
                f"{event.get('icu_training_load')!r}"
            )
            failures += 1

    if failures:
        log_info(
            "Intervals.icu must generate workout_doc from description; never send a "
            "separate workout_doc in the event payload."
        )
        await cleanup_after_failure()
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
