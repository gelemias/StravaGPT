from __future__ import annotations

from datetime import date, timedelta

import pytest
from pydantic import ValidationError

from app.intervals.client import format_pace, threshold_pace_seconds_per_km
from app.intervals.errors import IntervalsAPIError, IntervalsValidationError
from app.intervals.models import (
    WorkoutSpec,
    assert_unique_external_ids,
    build_event,
    check_coros_window,
)
from app.intervals.workout_dsl import render_workout


TODAY = date(2026, 7, 26)


def spec(**overrides) -> WorkoutSpec:
    payload = {
        "date": "2026-07-27",
        "sport": "Run",
        "name": "Easy run",
        "target_type": "pace",
        "external_id": "w1-mon",
        "steps": [{"type": "work", "duration": "30min", "target": "easy"}],
    }
    payload.update(overrides)
    return WorkoutSpec(**payload)


# --------------------------------------------------------------------------
# COROS constraint 1: only about 7 days of planned workouts transfer
# --------------------------------------------------------------------------


def test_workout_inside_the_window_is_accepted():
    workouts = [spec(date=(TODAY + timedelta(days=day)).isoformat()) for day in range(0, 8)]
    assert check_coros_window(workouts, max_future_days=7, today=TODAY) == []


def test_workout_beyond_the_window_is_rejected():
    workouts = [spec(date=(TODAY + timedelta(days=8)).isoformat(), external_id="too-far")]

    with pytest.raises(IntervalsValidationError) as excinfo:
        check_coros_window(workouts, max_future_days=7, today=TODAY)

    message = str(excinfo.value)
    assert "too-far" in message
    assert "8 days ahead" in message
    assert "allow_beyond_coros_window=true" in message


def test_past_workout_is_rejected():
    workouts = [spec(date=(TODAY - timedelta(days=1)).isoformat(), external_id="yesterday")]

    with pytest.raises(IntervalsValidationError) as excinfo:
        check_coros_window(workouts, max_future_days=7, today=TODAY)

    assert "in the past" in str(excinfo.value)


def test_non_strict_mode_returns_warnings_instead_of_raising():
    workouts = [spec(date=(TODAY + timedelta(days=30)).isoformat(), external_id="far")]

    problems = check_coros_window(workouts, max_future_days=7, today=TODAY, strict=False)

    assert len(problems) == 1
    assert "far" in problems[0]


def test_window_is_configurable():
    workouts = [spec(date=(TODAY + timedelta(days=20)).isoformat())]
    assert check_coros_window(workouts, max_future_days=30, today=TODAY) == []


# --------------------------------------------------------------------------
# COROS constraint 2: never mix pace and HR targets in one workout
# --------------------------------------------------------------------------


def test_hr_target_inside_a_pace_workout_is_rejected():
    mixed = spec(
        target_type="pace",
        external_id="mixed",
        steps=[
            {"type": "warmup", "duration": "15min", "target": "easy"},
            {"type": "work", "duration": "20min", "target": "150bpm"},
        ],
    )

    with pytest.raises(IntervalsValidationError) as excinfo:
        render_workout(mixed)

    message = str(excinfo.value)
    assert "150bpm" in message
    assert "target_type='pace'" in message
    assert "step 2" in message


def test_pace_target_inside_an_hr_workout_is_rejected():
    mixed = spec(
        target_type="hr",
        external_id="mixed-hr",
        steps=[
            {"type": "warmup", "duration": "15min", "target": "easy"},
            {
                "type": "interval",
                "repeat": 4,
                "work": {"distance": "2000m", "target": "4:00/km"},
                "recovery": {"duration": "2min", "target": "easy"},
            },
        ],
    )

    with pytest.raises(IntervalsValidationError) as excinfo:
        render_workout(mixed)

    message = str(excinfo.value)
    assert "4:00/km" in message
    assert "target_type='hr'" in message
    assert "work" in message


def test_percentage_targets_are_valid_for_both_target_types():
    for target_type in ("pace", "hr"):
        rendered = render_workout(
            spec(target_type=target_type, steps=[{"type": "work", "duration": "20min", "target": "90%"}])
        )
        assert rendered.description == "- 20m 90% " + ("Pace" if target_type == "pace" else "HR")


def test_target_type_must_be_pace_or_hr():
    with pytest.raises(ValidationError):
        spec(target_type="power")


# --------------------------------------------------------------------------
# Input schema validation
# --------------------------------------------------------------------------


def test_interval_step_requires_a_work_block():
    with pytest.raises(ValidationError) as excinfo:
        spec(steps=[{"type": "interval", "repeat": 4}])
    assert "needs a 'work' block" in str(excinfo.value)


def test_only_warmup_and_cooldown_may_be_left_open():
    """Open steps end on the lap button; every other type needs a length."""
    assert spec(steps=[{"type": "warmup", "target": "easy"}]).steps[0].is_open
    assert spec(steps=[{"type": "cooldown", "target": "easy"}]).steps[0].is_open

    for step_type in ("work", "steady", "recovery", "rest"):
        with pytest.raises(ValidationError) as excinfo:
            spec(steps=[{"type": step_type, "target": "easy"}])
        assert "needs either 'duration' or 'distance'" in str(excinfo.value)
        assert "may be left open" in str(excinfo.value)


def test_a_warmup_with_a_duration_is_not_open():
    assert not spec(steps=[{"type": "warmup", "duration": "15min"}]).steps[0].is_open


def test_interval_blocks_cannot_be_left_open():
    with pytest.raises(ValidationError):
        spec(steps=[{"type": "interval", "repeat": 3, "work": {"target": "threshold"}}])


def test_plain_step_needs_exactly_one_of_duration_or_distance():
    with pytest.raises(ValidationError):
        spec(steps=[{"type": "work", "target": "easy"}])
    with pytest.raises(ValidationError) as excinfo:
        spec(steps=[{"type": "work", "duration": "10min", "distance": "2000m"}])
    assert "exactly one" in str(excinfo.value)


def test_interval_step_must_not_carry_its_own_duration():
    with pytest.raises(ValidationError) as excinfo:
        spec(
            steps=[
                {
                    "type": "interval",
                    "duration": "10min",
                    "work": {"duration": "3min", "target": "threshold"},
                }
            ]
        )
    assert "must not set 'duration'" in str(excinfo.value)


def test_unknown_fields_are_rejected():
    with pytest.raises(ValidationError):
        spec(intensity="hard")


def test_steps_cannot_be_empty():
    with pytest.raises(ValidationError):
        spec(steps=[])


def test_duplicate_external_ids_are_rejected():
    with pytest.raises(IntervalsValidationError) as excinfo:
        assert_unique_external_ids([spec(), spec()])
    assert "Duplicate external_id 'w1-mon'" in str(excinfo.value)


def test_sport_name_is_normalized_to_intervals_casing():
    assert spec(sport="run").sport == "Run"
    assert spec(sport="virtualride").sport == "VirtualRide"
    assert spec(sport="Skiing").sport == "Skiing"


def test_start_time_defaults_to_midnight_and_accepts_hh_mm():
    assert spec().start_date_local == "2026-07-27T00:00:00"
    assert spec(start_time_local="07:30").start_date_local == "2026-07-27T07:30:00"
    with pytest.raises(ValidationError):
        spec(start_time_local="25:00")


# --------------------------------------------------------------------------
# Event payload
# --------------------------------------------------------------------------


def test_build_event_payload_shape():
    workout = spec()
    rendered = render_workout(workout)

    event = build_event(workout, rendered.description, rendered.moving_time, rendered.target)

    assert event == {
        "category": "WORKOUT",
        "start_date_local": "2026-07-27T00:00:00",
        "type": "Run",
        "name": "Easy run",
        "description": "- 30m 70% Pace",
        # Intervals.icu parses the text itself; the documented form is
        # description-only, not a hand-built steps array.
        "workout_doc": {"description": "- 30m 70% Pace"},
        "moving_time": 1800,
        "target": "PACE",
        "external_id": "w1-mon",
    }


# --------------------------------------------------------------------------
# Sport settings and API errors
# --------------------------------------------------------------------------


def test_threshold_pace_is_read_from_metres_per_second():
    settings = [
        {"types": ["Ride"], "threshold_pace": 8.0},
        {"types": ["Run", "VirtualRun"], "threshold_pace": 4.0},
    ]
    assert threshold_pace_seconds_per_km(settings, "Run") == pytest.approx(250.0)
    assert threshold_pace_seconds_per_km(settings, "run") == pytest.approx(250.0)
    assert threshold_pace_seconds_per_km(settings, "Swim") is None


def test_threshold_pace_ignores_missing_or_absurd_values():
    assert threshold_pace_seconds_per_km([{"types": ["Run"]}], "Run") is None
    assert threshold_pace_seconds_per_km([{"types": ["Run"], "threshold_pace": 0}], "Run") is None
    assert threshold_pace_seconds_per_km("nonsense", "Run") is None


def test_inspect_step_targets_flags_a_power_target():
    """A step that resolved to power means a percentage lost its Pace qualifier."""
    from app.intervals.client import inspect_step_targets

    report = inspect_step_targets(
        {
            "workout_doc": {"steps": [{"power": {"start": 10, "end": 10, "units": "%ftp"}}]},
            "icu_training_load": 0,
        }
    )

    assert report["has_workout_doc"] is True
    assert report["step_count"] == 1
    assert report["mentions_power"] is True
    assert report["training_load"] == 0


def test_inspect_step_targets_reports_pace_and_load():
    from app.intervals.client import inspect_step_targets

    report = inspect_step_targets(
        {
            "workout_doc": {
                "steps": [
                    {"distance": 2000, "pace": {"start": 3.9, "end": 4.0, "units": "MPS"}}
                ]
            },
            "icu_training_load": 87,
        }
    )

    assert report["mentions_pace"] is True
    assert report["mentions_power"] is False
    assert report["training_load"] == 87
    assert report["load_fields"] == {"icu_training_load": 87}


def test_inspect_step_targets_handles_a_missing_workout_doc():
    from app.intervals.client import inspect_step_targets

    report = inspect_step_targets({"id": 1})

    assert report["has_workout_doc"] is False
    assert report["step_count"] is None
    assert report["training_load"] == 0


def test_format_pace():
    assert format_pace(250.0) == "4:10/km"
    assert format_pace(305.4) == "5:05/km"


def test_api_error_message_is_readable_and_includes_a_hint():
    error = IntervalsAPIError("POST", "/athlete/0/events/bulk", 401, {"error": "nope"})
    message = str(error)
    assert "401" in message
    assert "POST /athlete/0/events/bulk" in message
    assert "INTERVALS_API_KEY" in message
    assert "nope" in message


def test_api_error_truncates_long_bodies():
    error = IntervalsAPIError("GET", "/athlete/0/events", 500, "x" * 5000)
    message = str(error)
    assert len(message) < 700
    assert message.endswith("...")
    assert "server error" in message
