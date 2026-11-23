import time

import pytest

pytest.importorskip("pyspark")
fakeredis = pytest.importorskip("fakeredis")
pytest.importorskip("redis")

from pyspark.sql import Row

import streaming.batch_sink as batch_sink
from streaming.redis_sink import read_area_stats


def test_partition_replay_after_partial_failure_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        batch_sink.Redis,
        "from_url",
        lambda url: fakeredis.FakeRedis(server=server),
    )
    cell = "882a107289fffff"
    minute = int(time.time()) // 60 * 60
    rows = [
        Row(
            h3_cell=cell,
            minute_start=minute,
            vehicle_id=vehicle,
            recorded_at_us=minute * 1_000_000 + 10_000_000,
            sequence_number=1,
            speed_kmh=speed,
        )
        for vehicle, speed in (("a", 60.0), ("b", 10.0))
    ]
    original_store = batch_sink.store_vehicle_speed
    attempts = 0

    def fail_on_second(client, **observation):
        nonlocal attempts
        attempts += 1
        if attempts == 2:
            raise ConnectionError("Redis connection interrupted")
        return original_store(client, **observation)

    monkeypatch.setattr(batch_sink, "store_vehicle_speed", fail_on_second)
    with pytest.raises(ConnectionError, match="interrupted"):
        batch_sink.write_partition(iter(rows), "redis://test")

    monkeypatch.setattr(batch_sink, "store_vehicle_speed", original_store)
    batch_sink.write_partition(iter(rows), "redis://test")

    client = fakeredis.FakeRedis(server=server)
    try:
        assert read_area_stats(client, cell=cell, minute_start=minute) == (2, 35.0)
    finally:
        client.close()
