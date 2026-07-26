from __future__ import annotations

import pytest

from app.intervals.errors import IntervalsValidationError
from app.intervals.models import WorkoutSpec
from app.intervals.workout_dsl import (
    format_distance,
    format_duration,
    parse_distance,
    parse_duration,
    render_workout,
    resolve_target,
)


# --------------------------------------------------------------------------
# Duration and distance parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15min", 900),
        ("15m", 900),
        ("15 minutes", 900),
        ("90s", 90),
        ("1h", 3600),
        ("1h30m", 5400),
        ("2:30", 150),
        ("1:00:00", 3600),
    ],
)
def test_parse_duration(text, expected):
    assert parse_duration(text) == expected


@pytest.mark.parametrize("text", ["", "15", "fifteen", "15x", "1:2:3:4", "-5m"])
def test_parse_duration_rejects_ambiguous_input(text):
    with pytest.raises(IntervalsValidationError):
        parse_duration(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [("2000m", 2000.0), ("2km", 2000.0), ("5k", 5000.0), ("1mi", 1609.344)],
)
def test_parse_distance(text, expected):
    assert parse_distance(text) == pytest.approx(expected)


@pytest.mark.parametrize("text", ["", "2000", "2 furlongs", "abc"])
def test_parse_distance_rejects_bad_input(text):
    with pytest.raises(IntervalsValidationError):
        parse_distance(text)


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(900, "15m"), (90, "90s"), (30, "30s"), (3600, "1h"), (5430, "90m30s")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_format_distance_uses_meters():
    assert format_distance(2000.0) == "2000m"
    assert format_distance(1609.344) == "1609m"


# --------------------------------------------------------------------------
# Target parsing
# --------------------------------------------------------------------------


def test_resolve_target_percent_is_neutral():
    target = resolve_target("75%", "pace")
    assert target.text == "75%"
    assert target.kind == "percent"
    assert target.percent == 75


def test_resolve_target_percent_range():
    target = resolve_target("95-100%", "pace")
    assert target.text == "95-100%"
    assert target.percent == pytest.approx(97.5)


def test_resolve_target_absolute_pace_range():
    target = resolve_target("4:00-4:02/km", "pace")
    assert target.text == "4:00-4:02/km"
    assert target.kind == "pace"
    assert target.pace_seconds_per_km == pytest.approx(241.0)


def test_resolve_target_pace_per_mile_converts_to_seconds_per_km():
    target = resolve_target("6:26/mi", "pace")
    assert target.kind == "pace"
    assert target.pace_seconds_per_km == pytest.approx(386 * 1000 / 1609.344)


def test_resolve_target_heart_rate():
    target = resolve_target("150-155bpm", "hr")
    assert target.text == "150-155bpm"
    assert target.kind == "hr"


def test_resolve_target_alias_depends_on_target_type():
    assert resolve_target("easy", "pace").text == "70%"
    assert resolve_target("easy", "hr").text == "68%"
    assert resolve_target("Threshold", "pace").text == "100%"


def test_resolve_target_alias_keeps_original_as_source():
    assert resolve_target("vo2max", "pace").source == "vo2max"


def test_resolve_target_rejects_gibberish():
    with pytest.raises(IntervalsValidationError) as excinfo:
        resolve_target("very fast", "pace")
    assert "could not read the target" in str(excinfo.value)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_render_matches_native_intervals_syntax():
    """The user's own example, expressed in the input schema."""
    spec = WorkoutSpec(
        date="2026-07-29",
        sport="Run",
        name="Umbral 4x2km",
        target_type="pace",
        external_id="w2-wed",
        steps=[
            {"type": "warmup", "duration": "15min", "target": "easy"},
            {
                "type": "interval",
                "repeat": 4,
                "work": {"distance": "2000m", "target": "4:00-4:02/km"},
                "recovery": {"duration": "2min", "target": "easy"},
            },
            {"type": "cooldown", "duration": "10min", "target": "easy"},
        ],
    )

    rendered = render_workout(spec, threshold_pace_seconds_per_km=240.0)

    assert rendered.description == (
        "- 15m 70% Warmup\n"
        "\n"
        "4x\n"
        "- 2000m 4:00-4:02/km\n"
        "- 2m 70%\n"
        "\n"
        "- 10m 70% Cooldown"
    )
    assert rendered.target == "PACE"


def test_render_blank_lines_group_consecutive_plain_steps():
    """Mirrors the shape of the reference example from the Intervals.icu docs."""
    spec = WorkoutSpec(
        date="2026-07-29",
        name="Reference shape",
        target_type="pace",
        external_id="ref",
        steps=[
            {"type": "warmup", "duration": "15m", "target": "55%"},
            {
                "type": "interval",
                "repeat": 3,
                "work": {"duration": "1m", "target": "150%"},
                "recovery": {"duration": "1m", "target": "50%"},
            },
            {"type": "work", "duration": "5m", "target": "50%"},
            {"type": "work", "duration": "5m", "target": "120%"},
            {"type": "work", "duration": "15m", "target": "55%"},
        ],
    )

    assert render_workout(spec).description == (
        "- 15m 55% Warmup\n"
        "\n"
        "3x\n"
        "- 1m 150%\n"
        "- 1m 50%\n"
        "\n"
        "- 5m 50%\n"
        "- 5m 120%\n"
        "- 15m 55%"
    )


def test_render_omits_repeat_header_for_single_repetition():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="Single",
        target_type="pace",
        external_id="single",
        steps=[
            {
                "type": "interval",
                "repeat": 1,
                "work": {"duration": "10m", "target": "threshold"},
            }
        ],
    )
    assert render_workout(spec).description == "- 10m 100%"


def test_moving_time_sums_time_steps_and_repeats():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="Timed",
        target_type="pace",
        external_id="timed",
        steps=[
            {"type": "warmup", "duration": "10min", "target": "easy"},
            {
                "type": "interval",
                "repeat": 3,
                "work": {"duration": "3min", "target": "threshold"},
                "recovery": {"duration": "1min", "target": "easy"},
            },
            {"type": "cooldown", "duration": "5min", "target": "easy"},
        ],
    )
    # 600 + 3 * (180 + 60) + 300
    assert render_workout(spec).moving_time == 1620


def test_moving_time_uses_absolute_pace_for_distance_steps():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="By distance",
        target_type="pace",
        external_id="dist",
        steps=[
            {
                "type": "interval",
                "repeat": 2,
                "work": {"distance": "2000m", "target": "4:00/km"},
            }
        ],
    )
    # 2 km at 240 s/km = 480 s per rep, twice.
    assert render_workout(spec).moving_time == 960


def test_moving_time_derives_pace_from_threshold_percentage():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="Percent distance",
        target_type="pace",
        external_id="pct",
        steps=[{"type": "work", "distance": "1000m", "target": "100%"}],
    )
    assert render_workout(spec, threshold_pace_seconds_per_km=240.0).moving_time == 240

    faster = WorkoutSpec(
        date="2026-07-29",
        name="Percent distance",
        target_type="pace",
        external_id="pct",
        steps=[{"type": "work", "distance": "1000m", "target": "120%"}],
    )
    assert render_workout(faster, threshold_pace_seconds_per_km=240.0).moving_time == 200


def test_distance_step_without_threshold_pace_warns():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="No threshold",
        target_type="pace",
        external_id="nothr",
        steps=[{"type": "work", "distance": "1000m", "target": "100%"}],
    )
    rendered = render_workout(spec, threshold_pace_seconds_per_km=None)
    assert rendered.moving_time == 300
    assert any("threshold pace" in warning for warning in rendered.warnings)


def test_explicit_moving_time_overrides_the_estimate():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="Override",
        target_type="pace",
        external_id="override",
        moving_time=1234,
        steps=[{"type": "work", "duration": "10min", "target": "easy"}],
    )
    assert render_workout(spec).moving_time == 1234


def test_hr_workout_renders_bpm_and_hr_target():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="Aerobic",
        target_type="hr",
        external_id="hr1",
        steps=[
            {"type": "warmup", "duration": "10min", "target": "easy"},
            {"type": "work", "duration": "40min", "target": "140-150bpm"},
        ],
    )
    rendered = render_workout(spec)
    assert rendered.target == "HR"
    assert rendered.description == "- 10m 68% Warmup\n- 40m 140-150bpm"


def test_notes_are_appended_after_the_steps():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="With notes",
        target_type="pace",
        external_id="notes",
        notes="Hydrate before starting.",
        steps=[{"type": "work", "duration": "30min", "target": "easy"}],
    )
    assert render_workout(spec).description == (
        "- 30m 70%\n\nHydrate before starting."
    )


def test_custom_label_is_appended_to_the_step_line():
    spec = WorkoutSpec(
        date="2026-07-29",
        name="Labelled",
        target_type="pace",
        external_id="label",
        steps=[{"type": "work", "duration": "20min", "target": "tempo", "label": "Tempo block"}],
    )
    assert render_workout(spec).description == "- 20m 90% Tempo block"
