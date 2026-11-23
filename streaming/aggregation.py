from pyspark.sql import DataFrame, functions as F
from pyspark.sql.types import StringType

from app.geo import location_cell


def latest_vehicle_speeds(pings: DataFrame) -> DataFrame:
    """Keep the latest validated ping for each vehicle, H3 cell and minute."""
    cell = F.udf(location_cell, StringType())
    return (
        pings.withColumn("h3_cell", cell("latitude", "longitude"))
        .withWatermark("recorded_at", "1 minute")
        .groupBy(
            F.window("recorded_at", "1 minute").alias("minute"),
            "h3_cell",
            "vehicle_id",
        )
        .agg(
            F.max_by(
                F.struct("recorded_at", "sequence_number", "speed_kmh"),
                F.struct("recorded_at", "sequence_number", "speed_kmh"),
            ).alias("latest")
        )
        .select(
            "minute",
            "h3_cell",
            "vehicle_id",
            "latest.recorded_at",
            "latest.sequence_number",
            "latest.speed_kmh",
        )
    )
