from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.models import PingCreate


def test_ping_accepts_valid_coordinates_and_adds_utc_timestamp() -> None:
    ping = PingCreate(
        vehicle_id="vehicle-00001",
        sequence_number=1,
        latitude=40.7128,
        longitude=-74.0060,
        speed_kmh=32.5,
    )

    assert ping.recorded_at.tzinfo == timezone.utc


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latitude", 91),
        ("longitude", -181),
        ("speed_kmh", -1),
        ("sequence_number", 0),
    ],
)
def test_ping_rejects_out_of_range_values(field: str, value: float) -> None:
    data = {
        "vehicle_id": "vehicle-00001",
        "sequence_number": 1,
        "latitude": 40.7128,
        "longitude": -74.0060,
        "speed_kmh": 32.5,
        field: value,
    }

    with pytest.raises(ValidationError):
        PingCreate(**data)


def test_ping_rejects_timestamp_without_timezone() -> None:
    with pytest.raises(ValidationError):
        PingCreate(
            vehicle_id="vehicle-00001",
            sequence_number=1,
            latitude=40.7128,
            longitude=-74.0060,
            speed_kmh=32.5,
            recorded_at=datetime(2026, 1, 1),
        )
