from functools import partial

from pyspark.sql import DataFrame
from pyspark.sql.streaming import DataStreamWriter

from streaming.aggregation import latest_vehicle_speeds
from streaming.batch_sink import write_redis_batch


def live_stats_writer(
    pings: DataFrame, *, redis_url: str, checkpoint_location: str
) -> DataStreamWriter:
    if not redis_url:
        raise ValueError("redis_url is required")
    if not checkpoint_location:
        raise ValueError("checkpoint_location is required")
    return (
        latest_vehicle_speeds(pings)
        .writeStream.outputMode("update")
        .option("checkpointLocation", checkpoint_location)
        .foreachBatch(partial(write_redis_batch, redis_url=redis_url))
    )
