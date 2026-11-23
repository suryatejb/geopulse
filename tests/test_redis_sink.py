import os
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import h3
import pytest

fakeredis = pytest.importorskip("fakeredis")
pytest.importorskip("redis")
from redis import Redis

from streaming.redis_sink import RETENTION_SECONDS, read_area_stats, store_vehicle_speed


@pytest.fixture
def redis_case():
    url = os.getenv("GEOPULSE_TEST_REDIS_URL")
    if url:
        client = Redis.from_url(url, decode_responses=True)
    else:
        client = fakeredis.FakeRedis(decode_responses=True)
    client.ping()
    latitude = secrets.randbelow(160_000_000) / 1_000_000 - 80
    longitude = secrets.randbelow(360_000_000) / 1_000_000 - 180
    cell = h3.latlng_to_cell(latitude, longitude, 8)
    minute = int(time.time()) // 60 * 60
    keys = [f"geopulse:stats:{minute}:{cell}", f"geopulse:stats:0:{cell}"]
    if client.exists(*keys):
        raise RuntimeError("Test Redis keys already exist")
    yield client, cell, minute, keys
    client.delete(*keys)
    client.close()


def test_latest_speed_updates_are_idempotent(redis_case) -> None:
    client, cell, minute, keys = redis_case
    assert read_area_stats(client, cell=cell, minute_start=minute) is None
    first = {
        "cell": cell,
        "minute_start": minute,
        "vehicle_id": "a",
        "recorded_at_us": minute * 1_000_000 + 20_000_000,
        "sequence_number": 2**53,
        "speed_kmh": 20.25,
    }
    assert store_vehicle_speed(client, **first)
    assert not store_vehicle_speed(client, **first)
    assert store_vehicle_speed(
        client, **{**first, "sequence_number": 2**53 + 1, "speed_kmh": 30.5}
    )
    assert not store_vehicle_speed(
        client, **{**first, "sequence_number": 2**53, "speed_kmh": 100.0}
    )
    assert not store_vehicle_speed(
        client, **{**first, "recorded_at_us": first["recorded_at_us"] - 1, "speed_kmh": 120.0}
    )
    assert store_vehicle_speed(
        client, **{**first, "sequence_number": 2**53 + 1, "speed_kmh": 35.5}
    )
    assert store_vehicle_speed(
        client,
        **{**first, "recorded_at_us": first["recorded_at_us"] + 1,
           "sequence_number": 1, "speed_kmh": 40.0},
    )
    assert store_vehicle_speed(
        client, **{**first, "vehicle_id": "b", "speed_kmh": 14.5}
    )
    stats = client.hgetall(keys[0])
    assert int(stats["vehicle_count"]) == 2
    assert float(stats["speed_sum"]) == pytest.approx(54.5)
    assert float(stats["speed_sum"]) / int(stats["vehicle_count"]) == pytest.approx(27.25)
    assert read_area_stats(client, cell=cell, minute_start=minute) == (2, 27.25)
    assert client.expiretime(keys[0]) == minute + 60 + RETENTION_SECONDS


def test_expired_minute_does_not_recreate_statistics(redis_case) -> None:
    client, cell, _, keys = redis_case
    assert not store_vehicle_speed(
        client,
        cell=cell,
        minute_start=0,
        vehicle_id="a",
        recorded_at_us=0,
        sequence_number=1,
        speed_kmh=1.0,
    )
    assert not client.exists(keys[1])


def test_read_area_stats_rejects_incomplete_data(redis_case) -> None:
    client, cell, minute, keys = redis_case
    client.hset(keys[0], "vehicle_count", 1)
    with pytest.raises(RuntimeError, match="incomplete"):
        read_area_stats(client, cell=cell, minute_start=minute)


def test_concurrent_retries_keep_one_latest_speed_per_vehicle(redis_case) -> None:
    client, cell, minute, _ = redis_case
    observations = [
        {
            "cell": cell,
            "minute_start": minute,
            "vehicle_id": f"vehicle-{vehicle}",
            "recorded_at_us": minute * 1_000_000 + sequence * 1_000_000,
            "sequence_number": sequence,
            "speed_kmh": float(sequence * 10),
        }
        for vehicle in range(6)
        for sequence in (2, 1, 2, 3)
    ]
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda value: store_vehicle_speed(client, **value), observations))
    assert read_area_stats(client, cell=cell, minute_start=minute) == (6, 30.0)


@pytest.mark.parametrize(
    "changes",
    [
        {"cell": "not-a-cell"},
        {"minute_start": 1},
        {"recorded_at_us": 60_000_000},
        {"vehicle_id": ""},
        {"sequence_number": 2**63},
        {"speed_kmh": float("nan")},
    ],
)
def test_invalid_observations_are_rejected(changes: dict) -> None:
    client = MagicMock(spec=Redis)
    args = {
        "cell": "882a107289fffff",
        "minute_start": 0,
        "vehicle_id": "a",
        "recorded_at_us": 0,
        "sequence_number": 1,
        "speed_kmh": 1.0,
    }
    with pytest.raises(ValueError):
        store_vehicle_speed(client, **{**args, **changes})
    client.eval.assert_not_called()
