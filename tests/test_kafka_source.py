import json
import logging
from datetime import datetime, timezone

import pytest

pytest.importorskip("pyspark")

from streaming.kafka_source import parse_stream_ping


def test_stream_ping_accepts_current_and_legacy_messages() -> None:
    payload = {
        "vehicle_id": "vehicle-1",
        "sequence_number": 7,
        "latitude": 40.7128,
        "longitude": -74.006,
        "speed_kmh": 20.5,
        "recorded_at": "2026-01-01T10:00:00Z",
    }
    expected = ("vehicle-1", 7, 40.7128, -74.006, 20.5, datetime(2026, 1, 1, 10, tzinfo=timezone.utc))
    assert parse_stream_ping(json.dumps(payload).encode(), "pings", 2, 17) == expected
    payload["h3_cell"] = "882a107289fffff"
    assert parse_stream_ping(bytearray(json.dumps(payload).encode()), "pings", 2, 18) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        b"\xff",
        b"{",
        b'{"vehicle_id":"vehicle-1","sequence_number":1,"latitude":0,"longitude":0,"speed_kmh":1}',
        b'{"vehicle_id":"vehicle-1","latitude":0,"longitude":0,"speed_kmh":1,"recorded_at":"2026-01-01T00:00:00Z"}',
        b'{"vehicle_id":"vehicle-1","sequence_number":1,"latitude":91,"longitude":0,"speed_kmh":1,"recorded_at":"2026-01-01T00:00:00Z"}',
        b'{"vehicle_id":"vehicle-1","sequence_number":1,"latitude":0,"longitude":0,"speed_kmh":1,"recorded_at":"2026-01-01T00:00:00"}',
    ],
)
def test_stream_ping_logs_and_skips_invalid_messages(
    caplog: pytest.LogCaptureFixture, value: bytes | None
) -> None:
    with caplog.at_level(logging.WARNING, logger="streaming.kafka_source"):
        assert parse_stream_ping(value, "pings", 2, 17) is None
    assert "topic=pings partition=2 offset=17" in caplog.text
