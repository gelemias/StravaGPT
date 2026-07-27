from __future__ import annotations

from datetime import date as date_type
from datetime import timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.intervals.errors import IntervalsValidationError


StepType = Literal["warmup", "cooldown", "interval", "work", "steady", "recovery", "rest"]

# Only these may be "open": no duration and no distance, so the athlete ends the
# step with the lap button on the watch. Every other type still needs a length,
# otherwise the workout has no defined structure.
OPEN_STEP_TYPES = frozenset({"warmup", "cooldown"})

TargetType = Literal["pace", "hr"]

# Intervals.icu activity types that make sense for a planned workout. Unknown
# values are passed through so a new sport does not need a code change.
COMMON_SPORTS = (
    "Run",
    "Ride",
    "Swim",
    "Walk",
    "Hike",
    "VirtualRide",
    "VirtualRun",
    "Rowing",
    "WeightTraining",
    "Workout",
    "Other",
)


class SimpleStep(BaseModel):
    """A single step: either a time or a distance, plus an intensity target."""

    model_config = ConfigDict(extra="forbid")

    duration: str | int | float | None = Field(
        default=None,
        description=(
            "Time-based duration, for example '15min', '90s', '1h30m' or '2:30'. "
            "Omit both duration and distance on a warmup/cooldown to leave the step "
            "open, ending when the athlete presses the lap button."
        ),
    )
    distance: str | int | float | None = Field(
        default=None,
        description="Distance-based duration, for example '2000m', '2km', '5k' or '1mi'.",
    )
    target: str | None = Field(
        default=None,
        description=(
            "Intensity: a percentage of threshold ('75%', '95-100%'), an absolute pace "
            "('4:00/km', '4:00-4:02/km'), a heart rate ('150bpm') or a name such as "
            "'easy', 'steady', 'tempo', 'threshold', 'vo2max'."
        ),
    )
    label: str | None = Field(
        default=None,
        description="Optional free text appended to the step line, for example 'Warmup'.",
    )

    def model_post_init(self, _context: object) -> None:
        # Reached only for the work/recovery blocks of an interval, which always
        # need a length. StepSpec overrides this with its own richer rules.
        if self.duration is None and self.distance is None:
            raise ValueError(
                "an interval 'work' or 'recovery' block needs either 'duration' "
                "or 'distance'. Only a top-level warmup or cooldown may be left "
                "open for the lap button."
            )
        if self.duration is not None and self.distance is not None:
            raise ValueError(
                "an interval block has both 'duration' and 'distance'. Use exactly one."
            )


class StepSpec(SimpleStep):
    """A top-level step. ``interval`` steps carry a repeated work/recovery block."""

    type: StepType = Field(description="Step kind.")
    repeat: int | None = Field(
        default=None,
        ge=1,
        le=99,
        description="Number of repetitions for an 'interval' step.",
    )
    work: SimpleStep | None = Field(
        default=None,
        description="The hard part of an 'interval' step. Required when type='interval'.",
    )
    recovery: SimpleStep | None = Field(
        default=None,
        description="The easy part of an 'interval' step.",
    )

    @field_validator("type")
    @classmethod
    def _normalize_type(cls, value: str) -> str:
        return value.lower()

    @property
    def is_open(self) -> bool:
        """True when the athlete ends this step with the lap button."""
        return (
            self.duration is None
            and self.distance is None
            and self.type in OPEN_STEP_TYPES
        )

    def model_post_init(self, _context: object) -> None:
        if self.type == "interval":
            if self.work is None:
                raise ValueError(
                    "type='interval' needs a 'work' block, for example "
                    "{\"work\": {\"distance\": \"2000m\", \"target\": \"threshold\"}}."
                )
            if self.duration is not None or self.distance is not None:
                raise ValueError(
                    "type='interval' must not set 'duration' or 'distance' directly; "
                    "put them inside 'work' and 'recovery'."
                )
        else:
            if self.work is not None or self.recovery is not None:
                raise ValueError(
                    f"type='{self.type}' must not set 'work' or 'recovery'; "
                    "use type='interval' for repeated blocks."
                )
            if (
                self.duration is None
                and self.distance is None
                and self.type not in OPEN_STEP_TYPES
            ):
                raise ValueError(
                    f"type='{self.type}' needs either 'duration' or 'distance'. "
                    f"Only {sorted(OPEN_STEP_TYPES)} may be left open for the lap button."
                )
            if self.duration is not None and self.distance is not None:
                raise ValueError(
                    f"type='{self.type}' has both 'duration' and 'distance'. Use exactly one."
                )


class WorkoutSpec(BaseModel):
    """One planned workout in the caller's own format."""

    model_config = ConfigDict(extra="forbid")

    date: date_type = Field(description="Calendar day for the workout, as YYYY-MM-DD.")
    name: str = Field(min_length=1, description="Workout name shown on the calendar.")
    external_id: str = Field(
        min_length=1,
        description=(
            "Your own primary key. The upsert matches on it, so pushing the same "
            "external_id again updates the workout instead of duplicating it."
        ),
    )
    target_type: TargetType = Field(
        description=(
            "'pace' or 'hr'. Exactly one per workout: COROS forces every block to the "
            "intensity type of the first one, so mixing them transfers incorrectly."
        )
    )
    steps: list[StepSpec] = Field(min_length=1, description="Ordered list of steps.")
    sport: str = Field(default="Run", description="Intervals.icu activity type, e.g. 'Run'.")
    notes: str | None = Field(
        default=None,
        description="Optional free text appended after the generated steps.",
    )
    moving_time: int | None = Field(
        default=None,
        ge=1,
        description="Override the estimated duration in seconds. Normally leave unset.",
    )
    start_time_local: str = Field(
        default="00:00:00",
        description="Local time of day for the event, as HH:MM:SS.",
    )

    @field_validator("target_type")
    @classmethod
    def _normalize_target_type(cls, value: str) -> str:
        return value.lower()

    @field_validator("sport")
    @classmethod
    def _normalize_sport(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("sport must not be empty.")
        for known in COMMON_SPORTS:
            if known.lower() == cleaned.lower():
                return known
        return cleaned

    @field_validator("start_time_local")
    @classmethod
    def _validate_start_time(cls, value: str) -> str:
        parts = value.strip().split(":")
        if len(parts) == 2:
            parts.append("00")
        if len(parts) != 3 or not all(part.isdigit() for part in parts):
            raise ValueError("start_time_local must look like 'HH:MM' or 'HH:MM:SS'.")
        hours, minutes, seconds = (int(part) for part in parts)
        if not (0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 60):
            raise ValueError("start_time_local is not a valid time of day.")
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    @property
    def start_date_local(self) -> str:
        return f"{self.date.isoformat()}T{self.start_time_local}"


def build_event(spec: WorkoutSpec, description: str, moving_time: int, target: str) -> dict:
    """Build an event whose description Intervals.icu parses into workout steps.

    Do not send ``workout_doc``. Intervals.icu generates that structured value
    server-side from the description; supplying a separate document prevents
    the parser from creating sets for this integration.
    """
    return {
        "category": "WORKOUT",
        "start_date_local": spec.start_date_local,
        "type": spec.sport,
        "name": spec.name,
        "description": description,
        "moving_time": moving_time,
        "target": target,
        "external_id": spec.external_id,
    }


def assert_unique_external_ids(specs: list[WorkoutSpec]) -> None:
    seen: dict[str, int] = {}
    for index, spec in enumerate(specs):
        if spec.external_id in seen:
            raise IntervalsValidationError(
                f"Duplicate external_id {spec.external_id!r} at positions "
                f"{seen[spec.external_id] + 1} and {index + 1}. Each workout in a batch "
                "needs its own external_id, otherwise the upsert overwrites itself."
            )
        seen[spec.external_id] = index


def check_coros_window(
    specs: list[WorkoutSpec],
    *,
    max_future_days: int,
    today: date_type,
    strict: bool = True,
) -> list[str]:
    """Enforce the COROS planned-workout horizon.

    COROS only picks up roughly a week of planned workouts. In strict mode a
    workout outside ``[today, today + max_future_days]`` raises; otherwise the
    problem is returned as a warning so the caller can decide.
    """
    horizon = today + timedelta(days=max_future_days)
    problems: list[str] = []

    for spec in specs:
        if spec.date < today:
            problems.append(
                f"Workout {spec.external_id!r} is dated {spec.date.isoformat()}, which is in "
                f"the past (today is {today.isoformat()}). COROS will not pick it up."
            )
        elif spec.date > horizon:
            days_ahead = (spec.date - today).days
            problems.append(
                f"Workout {spec.external_id!r} is dated {spec.date.isoformat()}, "
                f"{days_ahead} days ahead. COROS only syncs about {max_future_days} days of "
                f"planned workouts (up to {horizon.isoformat()}), so it would stay in "
                "Intervals.icu without reaching the watch."
            )

    if problems and strict:
        raise IntervalsValidationError(
            "Refusing to push workouts outside the COROS sync window:\n- "
            + "\n- ".join(problems)
            + "\nPush them closer to the date, or set allow_beyond_coros_window=true to "
            "store them in Intervals.icu anyway."
        )
    return problems
