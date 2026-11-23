import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiokafka.errors import MessageSizeTooLargeError
from aiokafka.structs import ConsumerRecord, TopicPartition
from pydantic import ValidationError

from app.kafka_writer import (
    dlq_payload,
    parse_ping,
    retry_database_insert,
    retry_dlq_publish,
    run_writer,
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


def test_parse_ping_accepts_message_with_h3_cell() -> None:
    ping = parse_ping(
        json.dumps(
            {
                "vehicle_id": "vehicle-00001",
                "sequence_number": 7,
                "latitude": 40.7128,
                "longitude": -74.006,
                "speed_kmh": 32.5,
                "h3_cell": "882a107289fffff",
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


def make_record(value: bytes | None) -> ConsumerRecord[bytes, bytes]:
    return ConsumerRecord(
        topic="pings",
        partition=2,
        offset=17,
        timestamp=0,
        timestamp_type=0,
        key=b"vehicle",
        value=value,
        checksum=None,
        serialized_key_size=7,
        serialized_value_size=len(value) if value is not None else -1,
        headers=[],
    )


def test_dlq_payload_keeps_original_and_reason() -> None:
    payload = json.loads(dlq_payload(make_record(b'{"bad":'), "invalid JSON").decode())
    assert payload["reason"] == "invalid JSON"
    assert payload["original"] == '{"bad":'
    assert payload["source"] == {"topic": "pings", "partition": 2, "offset": 17}
    assert base64.b64decode(payload["original_base64"]) == b'{"bad":'


@pytest.mark.parametrize("value", [b"\xff\x00\x80", b"", None])
def test_dlq_preserves_original_binary_and_null(value: bytes | None) -> None:
    payload = json.loads(dlq_payload(make_record(value), "invalid").decode())
    encoded = payload["original_base64"]
    assert (base64.b64decode(encoded) if encoded is not None else None) == value


@pytest.mark.parametrize("value", [b"\xff\x00", b"", None])
def test_writer_publishes_original_record_before_committing(
    monkeypatch: pytest.MonkeyPatch, value: bytes | None
) -> None:
    record = make_record(value)
    producer = MagicMock()
    producer.start = AsyncMock()
    producer.stop = AsyncMock()
    producer.send_and_wait = AsyncMock()
    consumer = MagicMock()
    consumer.start = AsyncMock()
    consumer.stop = AsyncMock()
    consumer.commit = AsyncMock()

    async def records():
        yield record

    consumer.__aiter__.side_effect = records
    pool = MagicMock()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr("app.kafka_writer.AIOKafkaProducer", lambda **kwargs: producer)
    monkeypatch.setattr("app.kafka_writer.AIOKafkaConsumer", lambda *args, **kwargs: consumer)
    monkeypatch.setattr("app.kafka_writer.ConnectionPool", lambda **kwargs: pool)
    monkeypatch.setattr("app.kafka_writer.initialize_database", lambda pool: None)

    async def commit_after_publish(offsets: dict) -> None:
        assert producer.send_and_wait.await_count == 1

    consumer.commit.side_effect = commit_after_publish
    asyncio.run(run_writer())

    call = producer.send_and_wait.await_args
    assert call.args == ("pings-dlq",)
    assert call.kwargs["key"] == record.key
    payload = json.loads(call.kwargs["value"])
    encoded = payload["original_base64"]
    assert (base64.b64decode(encoded) if encoded is not None else None) == value
    assert payload["source"] == {"topic": "pings", "partition": 2, "offset": 17}
    consumer.commit.assert_awaited_once_with({TopicPartition("pings", 2): 18})


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


def test_retry_dlq_publish_does_not_succeed_when_message_is_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopping = asyncio.Event()
    producer = MagicMock()

    async def reject_message(*args: object, **kwargs: object) -> None:
        stopping.set()
        raise MessageSizeTooLargeError("DLQ message is too large")

    producer.send_and_wait = AsyncMock(side_effect=reject_message)
    original_sleep = asyncio.sleep
    monkeypatch.setattr("app.kafka_writer.asyncio.sleep", lambda _: original_sleep(0))

    result = asyncio.run(
        retry_dlq_publish(producer, make_record(b"bad"), b"payload", stopping)
    )
    assert not result
    producer.send_and_wait.assert_awaited_once()
