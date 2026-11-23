import json
import logging
from datetime import datetime

from pydantic import ValidationError
from pyspark.sql import DataFrame, SparkSession, functions as F
from pyspark.sql.types import (
    DoubleType,
    LongType,
    StringType,
    StructField,
    StructType,
    TimestampType,
)

from app.ping_parser import parse_ping

logger = logging.getLogger(__name__)

PING_SCHEMA = StructType(
    [
        StructField("vehicle_id", StringType(), False),
        StructField("sequence_number", LongType(), False),
        StructField("latitude", DoubleType(), False),
        StructField("longitude", DoubleType(), False),
        StructField("speed_kmh", DoubleType(), False),
        StructField("recorded_at", TimestampType(), False),
    ]
)


def parse_stream_ping(
    value: bytes | bytearray | None, topic: str, partition: int, offset: int
) -> tuple[str, int, float, float, float, datetime] | None:
    try:
        ping = parse_ping(bytes(value) if value is not None else None)
        if "recorded_at" not in ping.model_fields_set:
            raise ValueError("A streaming ping must include recorded_at")
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as error:
        logger.warning(
            "Skipping invalid streaming ping topic=%s partition=%s offset=%s error=%s",
            topic,
            partition,
            offset,
            type(error).__name__,
        )
        return None

    if ping.sequence_number is None:
        raise RuntimeError("Validated Kafka ping is missing its sequence number")
    return (
        ping.vehicle_id,
        ping.sequence_number,
        ping.latitude,
        ping.longitude,
        ping.speed_kmh,
        ping.recorded_at,
    )


def parse_kafka_records(records: DataFrame) -> DataFrame:
    parse = F.udf(parse_stream_ping, PING_SCHEMA)
    return (
        records.select(parse("value", "topic", "partition", "offset").alias("ping"))
        .where(F.col("ping").isNotNull())
        .select("ping.*")
    )


def read_kafka_pings(spark: SparkSession, *, bootstrap_servers: str, topic: str) -> DataFrame:
    if not bootstrap_servers:
        raise ValueError("bootstrap_servers is required")
    if not topic:
        raise ValueError("topic is required")
    records = (
        spark.readStream.format("kafka")
        .option("kafka.bootstrap.servers", bootstrap_servers)
        .option("subscribe", topic)
        .option("startingOffsets", "earliest")
        .load()
    )
    return parse_kafka_records(records)
