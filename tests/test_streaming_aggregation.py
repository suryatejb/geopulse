from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import base64
import json
import os
import sys

import pytest

pyspark = pytest.importorskip("pyspark")
from pyspark.sql import SparkSession
from pyspark.errors import StreamingQueryException

from streaming.aggregation import latest_vehicle_speeds


@pytest.fixture(scope="module")
def spark() -> Iterator[SparkSession]:
    os.environ["PYSPARK_PYTHON"] = sys.executable
    session = (
        SparkSession.builder.master("local[1]")
        .appName("geopulse-aggregation-test")
        .config("spark.ui.enabled", "false")
        .config("spark.driver.memory", "512m")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.driver.host", "127.0.0.1")
        .config("spark.driver.bindAddress", "127.0.0.1")
        .getOrCreate()
    )
    yield session
    session.stop()


def test_latest_speed_per_vehicle_area_and_minute(spark: SparkSession) -> None:
    rows = [
        ("a", 1, 40.7128, -74.006, 20.0, datetime(2026, 1, 1, 10, 0, 10)),
        ("a", 2, 40.7128, -74.006, 50.0, datetime(2026, 1, 1, 10, 0, 40)),
        ("a", 4, 40.7128, -74.006, 100.0, datetime(2026, 1, 1, 10, 0, 20)),
        ("a", 3, 40.7128, -74.006, 60.0, datetime(2026, 1, 1, 10, 0, 40)),
        ("b", 1, 40.7128, -74.006, 10.0, datetime(2026, 1, 1, 10, 0, 20)),
        ("a", 5, 40.7128, -74.006, 90.0, datetime(2026, 1, 1, 10, 1, 0)),
        ("a", 6, 40.7580, -73.9855, 70.0, datetime(2026, 1, 1, 10, 0, 30)),
    ]
    pings = spark.createDataFrame(
        rows,
        "vehicle_id string, sequence_number long, latitude double, "
        "longitude double, speed_kmh double, recorded_at timestamp",
    )

    results = latest_vehicle_speeds(pings).collect()
    first_minute = [
        row for row in results
        if row.minute.start == datetime(2026, 1, 1, 10, 0)
        and row.h3_cell == "882a107289fffff"
    ]
    assert {row.vehicle_id: row.speed_kmh for row in first_minute} == {
        "a": 60.0,
        "b": 10.0,
    }
    assert len(first_minute) == 2
    assert sum(row.speed_kmh for row in first_minute) / len(first_minute) == 35.0
    assert len(results) == 4
    assert any(row.minute.start == datetime(2026, 1, 1, 10, 1) for row in results)
    assert any(row.h3_cell != "882a107289fffff" for row in results)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Spark streaming checkpoints need Hadoop's native Windows libraries",
)
def test_streaming_late_data_and_checkpoint_restart(
    spark: SparkSession, tmp_path
) -> None:
    source = tmp_path / "input"
    source.mkdir()
    checkpoint = tmp_path / "checkpoint"
    results = []

    def run_batch(filename: str, values: list[dict]) -> None:
        (source / filename).write_text(
            "\n".join(json.dumps(value) for value in values), encoding="utf-8"
        )
        pings = (
            spark.readStream.schema(
                "vehicle_id string, sequence_number long, latitude double, "
                "longitude double, speed_kmh double, recorded_at timestamp"
            )
            .option("maxFilesPerTrigger", 1)
            .json(str(source))
        )

        def capture(batch, batch_id):
            results.extend(batch.collect())

        query = (
            latest_vehicle_speeds(pings)
            .writeStream.outputMode("update")
            .option("checkpointLocation", str(checkpoint))
            .foreachBatch(capture)
            .trigger(availableNow=True)
            .start()
        )
        if not query.awaitTermination(60):
            query.stop()
            pytest.fail("Spark streaming query did not finish")

    def ping(sequence: int, timestamp: str, speed: float) -> dict:
        return {
            "vehicle_id": "a",
            "sequence_number": sequence,
            "latitude": 40.7128,
            "longitude": -74.006,
            "speed_kmh": speed,
            "recorded_at": timestamp,
        }

    run_batch("01.json", [
        ping(1, "2026-01-01T10:00:40Z", 50.0),
        ping(3, "2026-01-01T10:01:10Z", 70.0),
    ])
    assert any(row.minute.start == datetime(2026, 1, 1, 10, 0)
               and row.speed_kmh == 50.0 for row in results)
    results.clear()

    run_batch("02.json", [ping(2, "2026-01-01T10:00:50Z", 60.0)])
    assert any(row.minute.start == datetime(2026, 1, 1, 10, 0)
               and row.speed_kmh == 60.0 for row in results)
    results.clear()

    run_batch("03.json", [ping(4, "2026-01-01T10:03:30Z", 80.0)])
    results.clear()
    run_batch("04.json", [ping(5, "2026-01-01T10:00:55Z", 90.0)])
    assert not results


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Spark streaming checkpoints need Hadoop's native Windows libraries",
)
def test_streaming_to_redis_retries_and_recovers(spark: SparkSession, tmp_path) -> None:
    redis_url = os.getenv("GEOPULSE_TEST_REDIS_URL")
    if not redis_url:
        pytest.skip("GEOPULSE_TEST_REDIS_URL must point to a dedicated test Redis")
    redis = pytest.importorskip("redis")
    from streaming.kafka_source import parse_kafka_records
    from streaming.pipeline import live_stats_writer
    from streaming.redis_sink import read_area_stats, stats_key

    client = redis.Redis.from_url(redis_url)
    minute = int(datetime.now(timezone.utc).timestamp()) // 60 * 60
    cell = "882a107289fffff"
    keys = [stats_key(cell, minute + offset) for offset in (0, 60, 180)]
    assert not client.exists(*keys), "test key already exists"
    source = tmp_path / "input"
    source.mkdir()
    checkpoint = tmp_path / "checkpoint"

    def ping(vehicle: str, sequence: int, seconds: int, speed: float) -> dict:
        timestamp = datetime.fromtimestamp(minute, timezone.utc) + timedelta(seconds=seconds)
        return {
            "vehicle_id": vehicle,
            "sequence_number": sequence,
            "latitude": 40.7128,
            "longitude": -74.006,
            "speed_kmh": speed,
            "recorded_at": timestamp.isoformat(),
        }

    def run_batch(
        filename: str | None, values: list[dict | bytes | None], url: str
    ) -> None:
        if filename is not None:
            records = []
            for offset, value in enumerate(values):
                if isinstance(value, dict):
                    value = json.dumps(value).encode()
                records.append({
                    "topic": "test-pings",
                    "partition": 0,
                    "offset": offset,
                    "value": base64.b64encode(value).decode() if value is not None else None,
                })
            (source / filename).write_text(
                "\n".join(json.dumps(record) for record in records), encoding="utf-8"
            )
        records = spark.readStream.schema(
            "topic string, partition int, offset long, value binary"
        ).option("maxFilesPerTrigger", 1).json(str(source))
        pings = parse_kafka_records(records)
        query = (
            live_stats_writer(
                pings, redis_url=url, checkpoint_location=str(checkpoint)
            )
            .trigger(availableNow=True)
            .start()
        )
        try:
            if not query.awaitTermination(60):
                pytest.fail("Spark streaming query did not finish")
        finally:
            if query.isActive:
                query.stop()

    try:
        run_batch("01.json", [
            ping("a", 1, 10, 20.0),
            ping("b", 1, 20, 10.0),
            ping("a", 3, 70, 70.0),
            b"\xff",
            None,
        ], redis_url)
        assert read_area_stats(client, cell=cell, minute_start=minute) == (2, 15.0)

        with pytest.raises(StreamingQueryException):
            run_batch(
                "02.json",
                [ping("a", 2, 50, 60.0), ping("a", 4, 30, 100.0)],
                "redis://127.0.0.1:1/0?socket_connect_timeout=1",
            )
        assert read_area_stats(client, cell=cell, minute_start=minute) == (2, 15.0)
        run_batch(None, [], redis_url)
        assert read_area_stats(client, cell=cell, minute_start=minute) == (2, 35.0)

        run_batch("03.json", [ping("a", 5, 210, 80.0)], redis_url)
        run_batch("04.json", [ping("a", 6, 55, 90.0)], redis_url)
        assert read_area_stats(client, cell=cell, minute_start=minute) == (2, 35.0)
    finally:
        client.delete(*keys)
        client.close()
