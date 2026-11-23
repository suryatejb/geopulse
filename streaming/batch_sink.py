from functools import partial
from typing import Iterable

from pyspark.sql import DataFrame, Row, functions as F
from redis import Redis

from streaming.redis_sink import store_vehicle_speed


def write_partition(rows: Iterable[Row], redis_url: str) -> None:
    client = Redis.from_url(redis_url)
    try:
        for row in rows:
            store_vehicle_speed(
                client,
                cell=row.h3_cell,
                minute_start=row.minute_start,
                vehicle_id=row.vehicle_id,
                recorded_at_us=row.recorded_at_us,
                sequence_number=row.sequence_number,
                speed_kmh=row.speed_kmh,
            )
    finally:
        client.close()


def write_redis_batch(batch: DataFrame, batch_id: int, redis_url: str) -> None:
    observations = batch.select(
        "h3_cell",
        F.unix_seconds("minute.start").alias("minute_start"),
        "vehicle_id",
        F.unix_micros("recorded_at").alias("recorded_at_us"),
        "sequence_number",
        "speed_kmh",
    )
    observations.foreachPartition(partial(write_partition, redis_url=redis_url))
