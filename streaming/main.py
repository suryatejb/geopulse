import os

from pyspark.sql import SparkSession

from streaming.kafka_source import read_kafka_pings
from streaming.pipeline import live_stats_writer


def main() -> None:
    redis_url = os.environ.get("REDIS_URL")
    checkpoint_location = os.environ.get("SPARK_CHECKPOINT_LOCATION")
    if not redis_url:
        raise RuntimeError("REDIS_URL is required for live statistics")
    if not checkpoint_location:
        raise RuntimeError("SPARK_CHECKPOINT_LOCATION is required for live statistics")

    builder = (
        SparkSession.builder.appName("geopulse-live-statistics")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.shuffle.partitions", os.getenv("SPARK_SHUFFLE_PARTITIONS", "2"))
    )
    master = os.getenv("SPARK_MASTER")
    if master:
        builder = builder.master(master)
    spark = builder.getOrCreate()
    try:
        pings = read_kafka_pings(
            spark,
            bootstrap_servers=os.getenv(
                "KAFKA_BOOTSTRAP_SERVERS",
                "geopulse-kafka-bootstrap.kafka.svc:9092",
            ),
            topic=os.getenv("KAFKA_TOPIC", "pings"),
        )
        query = live_stats_writer(
            pings,
            redis_url=redis_url,
            checkpoint_location=checkpoint_location,
        ).start()
        query.awaitTermination()
    finally:
        spark.stop()


if __name__ == "__main__":
    main()
