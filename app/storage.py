from __future__ import annotations

import json
import secrets
import sqlite3
import time
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any


AUTH_STATE_TTL_SECONDS = 10 * 60


SCHEMA_STATEMENTS = [
    """
    create table if not exists auth_states (
        state text primary key,
        created_at integer not null
    )
    """,
    """
    create table if not exists oauth_tokens (
        athlete_id integer primary key,
        access_token text not null,
        refresh_token text not null,
        expires_at integer not null,
        scope text,
        token_type text not null default 'Bearer',
        updated_at integer not null
    )
    """,
    """
    create table if not exists activities (
        id integer primary key,
        name text not null,
        sport_type text,
        activity_type text,
        distance_m real,
        moving_time_s integer,
        elapsed_time_s integer,
        total_elevation_gain_m real,
        average_speed_mps real,
        average_heartrate real,
        max_heartrate real,
        start_date text,
        start_date_local text,
        timezone text,
        raw_json text not null,
        synced_at integer not null
    )
    """,
    """
    create index if not exists idx_activities_start_date
        on activities(start_date)
    """,
]


UPSERT_ACTIVITY_SQL = """
insert into activities (
    id, name, sport_type, activity_type, distance_m, moving_time_s,
    elapsed_time_s, total_elevation_gain_m, average_speed_mps,
    average_heartrate, max_heartrate, start_date, start_date_local,
    timezone, raw_json, synced_at
)
values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
on conflict(id) do update set
    name = excluded.name,
    sport_type = excluded.sport_type,
    activity_type = excluded.activity_type,
    distance_m = excluded.distance_m,
    moving_time_s = excluded.moving_time_s,
    elapsed_time_s = excluded.elapsed_time_s,
    total_elevation_gain_m = excluded.total_elevation_gain_m,
    average_speed_mps = excluded.average_speed_mps,
    average_heartrate = excluded.average_heartrate,
    max_heartrate = excluded.max_heartrate,
    start_date = excluded.start_date,
    start_date_local = excluded.start_date_local,
    timezone = excluded.timezone,
    raw_json = excluded.raw_json,
    synced_at = excluded.synced_at
"""


@dataclass(frozen=True)
class StoredToken:
    athlete_id: int | None
    access_token: str
    refresh_token: str
    expires_at: int
    scope: str | None
    token_type: str


class Storage:
    def __init__(
        self,
        database_path: str,
        *,
        turso_database_url: str | None = None,
        turso_auth_token: str | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.turso_database_url = turso_database_url
        self.turso_auth_token = turso_auth_token

    @contextmanager
    def connect(self) -> Iterable[Any]:
        if self.turso_database_url:
            if not self.turso_auth_token:
                raise RuntimeError("Set TURSO_AUTH_TOKEN when TURSO_DATABASE_URL is configured.")
            try:
                import libsql
            except ImportError as exc:
                raise RuntimeError("Install the libsql package to use Turso storage.") from exc
            connection = libsql.connect(
                database=self.turso_database_url,
                auth_token=self.turso_auth_token,
            )
        else:
            self.database_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(self.database_path)
            connection.row_factory = sqlite3.Row

        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def init_db(self) -> None:
        with self.connect() as db:
            for statement in SCHEMA_STATEMENTS:
                db.execute(statement)

    def create_auth_state(self) -> str:
        state = secrets.token_urlsafe(24)
        with self.connect() as db:
            db.execute(
                "insert into auth_states (state, created_at) values (?, ?)",
                (state, int(time.time())),
            )
        return state

    def consume_auth_state(self, state: str) -> bool:
        cutoff = int(time.time()) - AUTH_STATE_TTL_SECONDS
        with self.connect() as db:
            db.execute("delete from auth_states where created_at < ?", (cutoff,))
            row = self._fetchone_dict(
                db,
                "select state from auth_states where state = ?",
                (state,),
            )
            if row is None:
                return False
            db.execute("delete from auth_states where state = ?", (state,))
            return True

    def save_token(self, token_payload: dict[str, Any]) -> StoredToken:
        athlete = token_payload.get("athlete") or {}
        athlete_id = athlete.get("id") or 0
        token = StoredToken(
            athlete_id=athlete_id,
            access_token=token_payload["access_token"],
            refresh_token=token_payload["refresh_token"],
            expires_at=int(token_payload["expires_at"]),
            scope=token_payload.get("scope"),
            token_type=token_payload.get("token_type", "Bearer"),
        )
        with self.connect() as db:
            db.execute(
                """
                insert into oauth_tokens (
                    athlete_id, access_token, refresh_token, expires_at, scope,
                    token_type, updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?)
                on conflict(athlete_id) do update set
                    access_token = excluded.access_token,
                    refresh_token = excluded.refresh_token,
                    expires_at = excluded.expires_at,
                    scope = excluded.scope,
                    token_type = excluded.token_type,
                    updated_at = excluded.updated_at
                """,
                (
                    token.athlete_id,
                    token.access_token,
                    token.refresh_token,
                    token.expires_at,
                    token.scope,
                    token.token_type,
                    int(time.time()),
                ),
            )
        return token

    def get_token(self) -> StoredToken | None:
        with self.connect() as db:
            row = self._fetchone_dict(
                db,
                """
                select athlete_id, access_token, refresh_token, expires_at, scope, token_type
                from oauth_tokens
                order by updated_at desc
                limit 1
                """,
            )
        if row is None:
            return None
        return StoredToken(
            athlete_id=row["athlete_id"],
            access_token=row["access_token"],
            refresh_token=row["refresh_token"],
            expires_at=row["expires_at"],
            scope=row["scope"],
            token_type=row["token_type"],
        )

    def upsert_activities(self, activities: list[dict[str, Any]]) -> int:
        if not activities:
            return 0
        synced_at = int(time.time())
        with self.connect() as db:
            for activity in activities:
                db.execute(UPSERT_ACTIVITY_SQL, self._activity_row(activity, synced_at))
        return len(activities)

    def list_activities(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        with self.connect() as db:
            return self._fetchall_dicts(
                db,
                """
                select id, name, sport_type, activity_type, distance_m, moving_time_s,
                    elapsed_time_s, total_elevation_gain_m, average_speed_mps,
                    average_heartrate, max_heartrate, start_date, start_date_local,
                    timezone, synced_at
                from activities
                order by start_date desc
                limit ? offset ?
                """,
                (limit, offset),
            )

    def summarize_training(self, since_epoch: int) -> dict[str, Any]:
        with self.connect() as db:
            row = self._fetchone_dict(
                db,
                """
                select
                    count(*) as activity_count,
                    coalesce(sum(distance_m), 0) as distance_m,
                    coalesce(sum(moving_time_s), 0) as moving_time_s,
                    coalesce(sum(total_elevation_gain_m), 0) as elevation_gain_m
                from activities
                where cast(strftime('%s', start_date) as integer) >= ?
                """,
                (since_epoch,),
            )
            by_sport = self._fetchall_dicts(
                db,
                """
                select
                    coalesce(sport_type, activity_type, 'Unknown') as sport,
                    count(*) as activity_count,
                    coalesce(sum(distance_m), 0) as distance_m,
                    coalesce(sum(moving_time_s), 0) as moving_time_s
                from activities
                where cast(strftime('%s', start_date) as integer) >= ?
                group by coalesce(sport_type, activity_type, 'Unknown')
                order by moving_time_s desc
                """,
                (since_epoch,),
            )

        assert row is not None
        return {
            "activity_count": row["activity_count"],
            "distance_m": row["distance_m"],
            "moving_time_s": row["moving_time_s"],
            "elevation_gain_m": row["elevation_gain_m"],
            "by_sport": by_sport,
        }

    def latest_activity_epoch(self) -> int | None:
        with self.connect() as db:
            row = self._fetchone_dict(
                db,
                "select max(cast(strftime('%s', start_date) as integer)) as latest from activities",
            )
        return int(row["latest"]) if row and row["latest"] is not None else None

    def _fetchone_dict(
        self,
        db: Any,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> dict[str, Any] | None:
        cursor = db.execute(query, params)
        row = cursor.fetchone()
        if row is None:
            return None
        return self._row_to_dict(row, cursor)

    def _fetchall_dicts(
        self,
        db: Any,
        query: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict[str, Any]]:
        cursor = db.execute(query, params)
        return [self._row_to_dict(row, cursor) for row in cursor.fetchall()]

    def _row_to_dict(self, row: Any, cursor: Any) -> dict[str, Any]:
        if isinstance(row, sqlite3.Row):
            return dict(row)
        if isinstance(row, dict):
            return row
        column_names = [column[0] for column in cursor.description]
        return dict(zip(column_names, row, strict=True))

    def _activity_row(self, activity: dict[str, Any], synced_at: int) -> tuple[Any, ...]:
        return (
            activity["id"],
            activity.get("name") or "Untitled activity",
            activity.get("sport_type"),
            activity.get("type"),
            activity.get("distance"),
            activity.get("moving_time"),
            activity.get("elapsed_time"),
            activity.get("total_elevation_gain"),
            activity.get("average_speed"),
            activity.get("average_heartrate"),
            activity.get("max_heartrate"),
            activity.get("start_date"),
            activity.get("start_date_local"),
            activity.get("timezone"),
            json.dumps(activity),
            synced_at,
        )
