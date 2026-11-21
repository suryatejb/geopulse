import json
import asyncio

import pytest
from pydantic import ValidationError

from app.kafka_writer import (
    dlq_payload,
    parse_ping,
    retry_database_insert,
    retry_dlq_publish,
    save_ping,
)
def test_parse_ping_accepts_valid_kafka_message() -> None:
    ping = parse_ping(
        json.dumps(
            {
                "vehicle_id": "vehicle-00001",
                "sequence_number": 7,
                "latitude": 40.7128,
                "longitude": -74.006,
                "speed_kmh": 32.5,
            }
        ).encode()
    )
    assert ping.vehicle_id == "vehicle-00001"
    assert ping.sequence_number == 7


@pytest.mark.parametrize(
    "value",
    [
        b"not-json",
        b'{"vehicle_id":"vehicle-00001","latitude":40}',
    ],
)
def test_parse_ping_rejects_malformed_or_unprocessable_message(value: bytes) -> None:
    with pytest.raises((json.JSONDecodeError, ValidationError)):
        parse_ping(value)


@pytest.mark.parametrize("sequence", [{}, {"sequence_number": None}])
def test_parse_ping_rejects_missing_or_null_sequence(sequence: dict) -> None:
    payload = {
        "vehicle_id": "vehicle-00001",
        "latitude": 40.7128,
        "longitude": -74.006,
        "speed_kmh": 32.5,
        **sequence,
    }
    with pytest.raises(ValueError, match="sequence_number"):
        parse_ping(json.dumps(payload).encode())


@pytest.mark.parametrize("value", [
    None,
    json.dumps({
        "vehicle_id": "vehicle-00001",
        "sequence_number": 2**63,
        "latitude": 40.7128,
        "longitude": -74.006,
        "speed_kmh": 32.5,
    }).encode(),
])
def test_parse_ping_rejects_tombstone_and_out_of_range_sequence(value: bytes | None) -> None:
    with pytest.raises(ValueError):
        parse_ping(value)


def test_dlq_payload_keeps_original_and_reason() -> None:
    payload = json.loads(dlq_payload(b'{"bad":', "invalid JSON").decode())
    assert payload["reason"] == "invalid JSON"
    assert payload["original"] == '{"bad":'


def test_save_ping_rejects_missing_sequence_number() -> None:
    ping = parse_ping(
        json.dumps(
            {
                "vehicle_id": "vehicle-00001",
                "sequence_number": 1,
                "latitude": 40.7128,
                "longitude": -74.006,
                "speed_kmh": 32.5,
            }
        ).encode()
    )
    ping.sequence_number = None
    with pytest.raises(ValueError, match="sequence_number"):
        save_ping(object(), ping)


def test_retry_database_insert_retries_same_record_until_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    ping = parse_ping(
        json.dumps(
            {
                "vehicle_id": "vehicle-00001",
                "sequence_number": 1,
                "latitude": 40.7128,
                "longitude": -74.006,
                "speed_kmh": 32.5,
            }
        ).encode()
    )
    record = type(
        "Record",
        (),
        {"topic": "pings", "partition": 2, "offset": 17},
    )()
    stopping = asyncio.Event()

    def flaky_save(pool: object, value: object) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("database unavailable")
        return True

    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.kafka_writer.save_ping", flaky_save)
    monkeypatch.setattr(
        "app.kafka_writer.asyncio.sleep",
        lambda _: original_sleep(0),
    )

    async def exercise() -> bool:
        return await retry_database_insert(object(), ping, record, stopping)

    assert asyncio.run(exercise())
    assert attempts == 2


def test_retry_dlq_publish_retries_same_record_until_success() -> None:
    attempts = 0

    class FlakyProducer:
        async def send_and_wait(
            self,
            topic: str,
            *,
            key: bytes,
            value: bytes,
        ) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError()

    record = type(
        "Record",
        (),
        {"topic": "pings", "partition": 3, "offset": 21, "key": b"vehicle-1"},
    )()

    async def exercise() -> bool:
        return await retry_dlq_publish(
            FlakyProducer(),
            record,
            b"bad-ping",
            asyncio.Event(),
        )

    assert asyncio.run(exercise())
    assert attempts == 2
