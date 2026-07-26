"""Convert a friendly workout JSON into the native Intervals.icu step syntax.

The native format is a plain-text ``description`` where every step is a line
starting with a dash, durations are either times (``15m``) or distances
(``1000m``), and repeats are declared with ``Nx`` on their own line before the
block to repeat::

    - 15m 55% Warmup

    3x
    - 1m 150%
    - 1m 50%

    - 5m 50%
    - 5m 120%
    - 15m 55%

Blank lines separate top-level groups; consecutive plain steps stay together.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.intervals.errors import IntervalsValidationError
from app.intervals.models import SimpleStep, WorkoutSpec


# --------------------------------------------------------------------------
# Intensity aliases
# --------------------------------------------------------------------------
# Percentages are relative to the athlete's threshold for the chosen target:
# threshold pace for "pace" workouts, LTHR for "hr" workouts. For pace a higher
# percentage means faster. These are deliberate, tunable heuristics - adjust
# them here (and only here) if they do not match how you train.

PACE_ALIASES: dict[str, str] = {
    "rest": "50%",
    "recovery": "60%",
    "easy": "70%",
    "endurance": "75%",
    "steady": "82%",
    "marathon": "85%",
    "tempo": "90%",
    "threshold": "100%",
    "interval": "108%",
    "vo2max": "112%",
    "repetition": "118%",
    "sprint": "125%",
}

HR_ALIASES: dict[str, str] = {
    "rest": "50%",
    "recovery": "60%",
    "easy": "68%",
    "endurance": "72%",
    "steady": "80%",
    "marathon": "85%",
    "tempo": "90%",
    "threshold": "98%",
    "interval": "102%",
    "vo2max": "105%",
    "repetition": "105%",
    "sprint": "105%",
}

ALIAS_TABLES = {"pace": PACE_ALIASES, "hr": HR_ALIASES}

# Fallback used to estimate moving_time for distance-based steps when neither an
# absolute pace target nor a threshold pace from sport-settings is available.
FALLBACK_THRESHOLD_PACE_SECONDS_PER_KM = 300.0

DEFAULT_STEP_INTENSITY = {
    "warmup": "easy",
    "cooldown": "easy",
    "rest": "rest",
    "recovery": "recovery",
    "steady": "steady",
    "work": "steady",
    "interval": "threshold",
}

DEFAULT_STEP_LABEL = {
    "warmup": "Warmup",
    "cooldown": "Cooldown",
}


# --------------------------------------------------------------------------
# Duration / distance parsing
# --------------------------------------------------------------------------

_TIME_UNITS = (
    ("hours", 3600),
    ("hour", 3600),
    ("hrs", 3600),
    ("hr", 3600),
    ("h", 3600),
    ("minutes", 60),
    ("minute", 60),
    ("mins", 60),
    ("min", 60),
    ("m", 60),
    ("seconds", 1),
    ("second", 1),
    ("secs", 1),
    ("sec", 1),
    ("s", 1),
)

_TIME_TOKEN = re.compile(
    r"(\d+(?:\.\d+)?)\s*(" + "|".join(unit for unit, _ in _TIME_UNITS) + r")",
    re.IGNORECASE,
)

_DISTANCE_UNITS = {
    "km": 1000.0,
    "k": 1000.0,
    "m": 1.0,
    "meter": 1.0,
    "meters": 1.0,
    "metre": 1.0,
    "metres": 1.0,
    "mi": 1609.344,
    "mile": 1609.344,
    "miles": 1609.344,
    "yd": 0.9144,
    "yds": 0.9144,
    "yard": 0.9144,
    "yards": 0.9144,
}

_DISTANCE_TOKEN = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(" + "|".join(sorted(_DISTANCE_UNITS, key=len, reverse=True)) + r")$",
    re.IGNORECASE,
)


def parse_duration(value: str | int | float, *, where: str = "duration") -> int:
    """Parse ``15min`` / ``15m`` / ``1h30m`` / ``90s`` / ``2:30`` into seconds."""
    if isinstance(value, bool):
        raise IntervalsValidationError(f"{where}: expected a duration, got a boolean.")
    if isinstance(value, (int, float)):
        seconds = int(round(float(value)))
        if seconds <= 0:
            raise IntervalsValidationError(f"{where}: duration must be greater than zero.")
        return seconds

    raw = str(value).strip().lower()
    if not raw:
        raise IntervalsValidationError(f"{where}: duration is empty.")

    if ":" in raw:
        chunks = raw.split(":")
        if len(chunks) not in (2, 3) or not all(chunk.strip().isdigit() for chunk in chunks):
            raise IntervalsValidationError(
                f"{where}: could not read the clock duration {value!r}. Use mm:ss or hh:mm:ss."
            )
        numbers = [int(chunk) for chunk in chunks]
        if len(numbers) == 2:
            seconds = numbers[0] * 60 + numbers[1]
        else:
            seconds = numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        if seconds <= 0:
            raise IntervalsValidationError(f"{where}: duration must be greater than zero.")
        return seconds

    matches = list(_TIME_TOKEN.finditer(raw))
    consumed = "".join(match.group(0) for match in matches)
    if not matches or len(consumed.replace(" ", "")) != len(raw.replace(" ", "")):
        raise IntervalsValidationError(
            f"{where}: could not read the duration {value!r}. "
            "Use a unit, for example '15min', '90s', '1h30m' or '2:30'."
        )

    seconds = 0.0
    for match in matches:
        amount = float(match.group(1))
        unit = match.group(2).lower()
        multiplier = next(value for name, value in _TIME_UNITS if name == unit)
        seconds += amount * multiplier
    total = int(round(seconds))
    if total <= 0:
        raise IntervalsValidationError(f"{where}: duration must be greater than zero.")
    return total


def parse_distance(value: str | int | float, *, where: str = "distance") -> float:
    """Parse ``2000m`` / ``2km`` / ``5k`` / ``1mi`` into meters."""
    if isinstance(value, bool):
        raise IntervalsValidationError(f"{where}: expected a distance, got a boolean.")
    if isinstance(value, (int, float)):
        meters = float(value)
        if meters <= 0:
            raise IntervalsValidationError(f"{where}: distance must be greater than zero.")
        return meters

    raw = str(value).strip().lower()
    match = _DISTANCE_TOKEN.match(raw)
    if match is None:
        raise IntervalsValidationError(
            f"{where}: could not read the distance {value!r}. "
            "Use a unit, for example '2000m', '2km', '5k' or '1mi'."
        )
    meters = float(match.group(1)) * _DISTANCE_UNITS[match.group(2).lower()]
    if meters <= 0:
        raise IntervalsValidationError(f"{where}: distance must be greater than zero.")
    return meters


def format_duration(seconds: int) -> str:
    """Render seconds using the shortest form Intervals.icu accepts."""
    seconds = int(seconds)
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    # Short odd durations read better as plain seconds ("90s" for a recovery).
    if seconds < 120:
        return f"{seconds}s"
    return f"{seconds // 60}m{seconds % 60}s"


def format_distance(meters: float) -> str:
    """Render a distance in meters, which every Intervals.icu parser accepts."""
    rounded = round(meters)
    if abs(meters - rounded) < 0.5:
        return f"{int(rounded)}m"
    return f"{meters:.1f}m"


# --------------------------------------------------------------------------
# Target parsing
# --------------------------------------------------------------------------

_PERCENT = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(?:-\s*(\d+(?:\.\d+)?)\s*)?%\s*"
    r"(?:of\s*)?(lthr|hr|max\s*hr|maxhr|ftp|threshold\s*pace|pace|threshold)?$",
    re.IGNORECASE,
)

_BPM = re.compile(r"^(\d{2,3})\s*(?:-\s*(\d{2,3})\s*)?(?:bpm|lthr|hr)$", re.IGNORECASE)

_PACE = re.compile(
    r"^(\d{1,2}:\d{2})\s*(?:-\s*(\d{1,2}:\d{2})\s*)?"
    r"(?:min\s*)?(?:/|per\s+)\s*(km|kms|kilometer|kilometre|mi|mile|100m|100yd)$",
    re.IGNORECASE,
)

_PACE_DISTANCE_SECONDS = {
    "km": 1000.0,
    "kms": 1000.0,
    "kilometer": 1000.0,
    "kilometre": 1000.0,
    "mi": 1609.344,
    "mile": 1609.344,
    "100m": 100.0,
    "100yd": 91.44,
}

PACE_KIND = "pace"
HR_KIND = "hr"
PERCENT_KIND = "percent"


@dataclass(frozen=True)
class Target:
    """A resolved intensity target.

    ``kind`` drives the COROS "do not mix pace and HR" validation:
    ``percent`` is neutral (valid for both), ``pace`` and ``hr`` are exclusive.
    """

    text: str
    kind: str
    percent: float | None = None
    pace_seconds_per_km: float | None = None
    source: str = ""


def _clock_to_seconds(clock: str) -> int:
    minutes, seconds = clock.split(":")
    return int(minutes) * 60 + int(seconds)


def _normalize_alias(raw: str) -> str:
    return re.sub(r"[\s_\-]+", "", raw.strip().lower())


def resolve_target(raw: str, target_type: str, *, where: str = "target") -> Target:
    """Turn a target string into a renderable token plus its metadata."""
    if raw is None:
        raise IntervalsValidationError(f"{where}: target is missing.")
    text = str(raw).strip()
    if not text:
        raise IntervalsValidationError(f"{where}: target is empty.")

    aliases = ALIAS_TABLES.get(target_type)
    if aliases is None:
        raise IntervalsValidationError(
            f"{where}: unknown target_type {target_type!r}. Use 'pace' or 'hr'."
        )

    alias_key = _normalize_alias(text)
    if alias_key in aliases:
        resolved = resolve_target(aliases[alias_key], target_type, where=where)
        return Target(
            text=resolved.text,
            kind=resolved.kind,
            percent=resolved.percent,
            pace_seconds_per_km=resolved.pace_seconds_per_km,
            source=text,
        )

    match = _PACE.match(text)
    if match is not None:
        low = _clock_to_seconds(match.group(1))
        high = _clock_to_seconds(match.group(2)) if match.group(2) else low
        unit = match.group(3).lower()
        per_km = 1000.0 / _PACE_DISTANCE_SECONDS[unit]
        mid_seconds_per_km = ((low + high) / 2) * per_km
        unit_text = "km" if unit in {"km", "kms", "kilometer", "kilometre"} else unit
        unit_text = "mi" if unit_text in {"mile"} else unit_text
        rendered = f"{match.group(1)}-{match.group(2)}/{unit_text}" if match.group(2) else f"{match.group(1)}/{unit_text}"
        return Target(
            text=rendered,
            kind=PACE_KIND,
            pace_seconds_per_km=mid_seconds_per_km,
            source=text,
        )

    match = _BPM.match(text)
    if match is not None:
        rendered = f"{match.group(1)}-{match.group(2)}bpm" if match.group(2) else f"{match.group(1)}bpm"
        return Target(text=rendered, kind=HR_KIND, source=text)

    match = _PERCENT.match(text)
    if match is not None:
        low = float(match.group(1))
        high = float(match.group(2)) if match.group(2) else low
        qualifier = (match.group(3) or "").lower().replace(" ", "")
        if qualifier in {"lthr", "hr", "maxhr"}:
            kind = HR_KIND
        elif qualifier in {"pace", "thresholdpace"}:
            kind = PACE_KIND
        else:
            kind = PERCENT_KIND
        low_text = match.group(1).rstrip("0").rstrip(".") if "." in match.group(1) else match.group(1)
        rendered = f"{low_text}%"
        if match.group(2):
            high_text = match.group(2).rstrip("0").rstrip(".") if "." in match.group(2) else match.group(2)
            rendered = f"{low_text}-{high_text}%"
        return Target(text=rendered, kind=kind, percent=(low + high) / 2, source=text)

    known = ", ".join(sorted(aliases))
    raise IntervalsValidationError(
        f"{where}: could not read the target {raw!r}. Accepted forms are "
        f"a percentage ('75%', '95-100%'), an absolute pace ('4:00/km', '4:00-4:02/km'), "
        f"a heart rate ('150bpm', '150-155bpm') or one of these names: {known}."
    )


def assert_target_matches_workout(target: Target, target_type: str, where: str) -> None:
    """Enforce the COROS rule: never mix pace and HR inside one workout."""
    if target_type == "pace" and target.kind == HR_KIND:
        raise IntervalsValidationError(
            f"{where}: target {target.source!r} is a heart-rate target but the workout "
            "declares target_type='pace'. COROS forces every block to the intensity type "
            "of the first one, so a workout must use pace targets only. "
            "Split it into two workouts or switch target_type to 'hr'."
        )
    if target_type == "hr" and target.kind == PACE_KIND:
        raise IntervalsValidationError(
            f"{where}: target {target.source!r} is a pace target but the workout "
            "declares target_type='hr'. COROS forces every block to the intensity type "
            "of the first one, so a workout must use heart-rate targets only. "
            "Split it into two workouts or switch target_type to 'pace'."
        )


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


@dataclass
class RenderedWorkout:
    description: str
    moving_time: int
    target: str
    warnings: list[str] = field(default_factory=list)


def _step_seconds(
    *,
    seconds: int | None,
    meters: float | None,
    target: Target,
    threshold_pace_seconds_per_km: float | None,
    where: str,
    warnings: list[str],
) -> int:
    if seconds is not None:
        return seconds

    assert meters is not None
    pace = target.pace_seconds_per_km
    if pace is None and target.percent and threshold_pace_seconds_per_km:
        # Higher percentage of threshold pace means faster, so fewer seconds per km.
        pace = threshold_pace_seconds_per_km / (target.percent / 100.0)
    if pace is None and threshold_pace_seconds_per_km:
        pace = threshold_pace_seconds_per_km
    if pace is None:
        pace = FALLBACK_THRESHOLD_PACE_SECONDS_PER_KM
        warnings.append(
            f"{where}: no absolute pace and no threshold pace available, estimated "
            f"moving_time using {format_duration(int(pace))}/km. Set your threshold pace "
            "in Intervals.icu Sport Settings for an accurate estimate."
        )
    return int(round(meters / 1000.0 * pace))


def _render_step_line(
    step: SimpleStep,
    *,
    step_type: str,
    target_type: str,
    threshold_pace_seconds_per_km: float | None,
    where: str,
    warnings: list[str],
) -> tuple[str, int]:
    duration = step.duration
    distance = step.distance

    if duration is None and distance is None:
        raise IntervalsValidationError(
            f"{where}: needs either a 'duration' (for example '15min') or a "
            "'distance' (for example '2000m')."
        )
    if duration is not None and distance is not None:
        raise IntervalsValidationError(
            f"{where}: has both 'duration' and 'distance'. Use exactly one."
        )

    seconds: int | None = None
    meters: float | None = None
    if duration is not None:
        seconds = parse_duration(duration, where=where)
        amount_text = format_duration(seconds)
    else:
        meters = parse_distance(distance, where=where)
        amount_text = format_distance(meters)

    raw_target = step.target
    if raw_target is None or str(raw_target).strip() == "":
        raw_target = DEFAULT_STEP_INTENSITY.get(step_type, "steady")
    target = resolve_target(raw_target, target_type, where=where)
    assert_target_matches_workout(target, target_type, where)

    label = step.label or DEFAULT_STEP_LABEL.get(step_type)

    line = f"- {amount_text} {target.text}"
    if label:
        line = f"{line} {label}"

    total = _step_seconds(
        seconds=seconds,
        meters=meters,
        target=target,
        threshold_pace_seconds_per_km=threshold_pace_seconds_per_km,
        where=where,
        warnings=warnings,
    )
    return line, total


def render_workout(
    spec: WorkoutSpec,
    *,
    threshold_pace_seconds_per_km: float | None = None,
) -> RenderedWorkout:
    """Render a workout spec into the native Intervals.icu description text."""
    target_type = spec.target_type
    warnings: list[str] = []
    groups: list[list[str]] = []
    current: list[str] = []
    total_seconds = 0

    for index, step in enumerate(spec.steps):
        position = f"workout '{spec.external_id}' step {index + 1} ({step.type})"

        if step.type == "interval":
            if step.work is None:
                raise IntervalsValidationError(
                    f"{position}: an 'interval' step needs a 'work' block."
                )
            if current:
                groups.append(current)
                current = []

            repeat = step.repeat or 1
            if repeat < 1:
                raise IntervalsValidationError(f"{position}: 'repeat' must be at least 1.")

            block: list[str] = [f"{repeat}x"] if repeat > 1 else []
            block_seconds = 0
            for part_name, part, part_type in (
                ("work", step.work, "interval"),
                ("recovery", step.recovery, "recovery"),
            ):
                if part is None:
                    continue
                line, seconds = _render_step_line(
                    part,
                    step_type=part_type,
                    target_type=target_type,
                    threshold_pace_seconds_per_km=threshold_pace_seconds_per_km,
                    where=f"{position} {part_name}",
                    warnings=warnings,
                )
                block.append(line)
                block_seconds += seconds

            groups.append(block)
            total_seconds += repeat * block_seconds
        else:
            line, seconds = _render_step_line(
                step,
                step_type=step.type,
                target_type=target_type,
                threshold_pace_seconds_per_km=threshold_pace_seconds_per_km,
                where=position,
                warnings=warnings,
            )
            current.append(line)
            total_seconds += seconds

    if current:
        groups.append(current)

    description = "\n\n".join("\n".join(group) for group in groups)
    if spec.notes:
        description = f"{description}\n\n{spec.notes}".strip()

    moving_time = spec.moving_time or total_seconds
    return RenderedWorkout(
        description=description,
        moving_time=int(moving_time),
        target="PACE" if target_type == "pace" else "HR",
        warnings=warnings,
    )
