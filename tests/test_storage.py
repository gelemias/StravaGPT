from app.storage import Storage


def test_upsert_and_list_activities(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    storage.init_db()

    count = storage.upsert_activities(
        [
            {
                "id": 1,
                "name": "Morning Run",
                "sport_type": "Run",
                "type": "Run",
                "distance": 5000.0,
                "moving_time": 1500,
                "elapsed_time": 1600,
                "total_elevation_gain": 45.0,
                "average_speed": 3.3,
                "average_heartrate": 145.0,
                "max_heartrate": 171.0,
                "start_date": "2026-06-01T06:00:00Z",
                "start_date_local": "2026-06-01T08:00:00Z",
                "timezone": "(GMT+01:00) Europe/Warsaw",
            }
        ]
    )

    assert count == 1
    activities = storage.list_activities()
    assert activities[0]["name"] == "Morning Run"
    assert activities[0]["distance_m"] == 5000.0


def test_training_summary(tmp_path):
    storage = Storage(str(tmp_path / "test.db"))
    storage.init_db()
    storage.upsert_activities(
        [
            {
                "id": 1,
                "name": "Run",
                "sport_type": "Run",
                "distance": 5000.0,
                "moving_time": 1500,
                "total_elevation_gain": 45.0,
                "start_date": "2026-06-01T06:00:00Z",
            },
            {
                "id": 2,
                "name": "Ride",
                "sport_type": "Ride",
                "distance": 20000.0,
                "moving_time": 3600,
                "total_elevation_gain": 180.0,
                "start_date": "2026-06-02T06:00:00Z",
            },
        ]
    )

    summary = storage.summarize_training(since_epoch=0)

    assert summary["activity_count"] == 2
    assert summary["distance_m"] == 25000.0
    assert summary["moving_time_s"] == 5100
    assert summary["elevation_gain_m"] == 225.0
    assert {row["sport"] for row in summary["by_sport"]} == {"Run", "Ride"}

