import h3
from redis import Redis

from app.geo import H3_RESOLUTION


def stats_key(cell: str, minute_start: int) -> str:
    if not h3.is_valid_cell(cell) or h3.get_resolution(cell) != H3_RESOLUTION:
        raise ValueError("cell must be a valid H3 resolution 8 cell")
    if minute_start % 60 != 0:
        raise ValueError("minute_start must be aligned to a UTC minute")
    return f"geopulse:stats:{minute_start}:{cell}"


def read_area_stats(
    client: Redis, *, cell: str, minute_start: int
) -> tuple[int, float] | None:
    count, speed_sum = client.hmget(
        stats_key(cell, minute_start), "vehicle_count", "speed_sum"
    )
    if count is None and speed_sum is None:
        return None
    if count is None or speed_sum is None:
        raise RuntimeError("Redis area statistics are incomplete")
    vehicle_count = int(count)
    if vehicle_count <= 0:
        raise RuntimeError("Redis area statistics have an invalid vehicle count")
    return vehicle_count, float(speed_sum) / vehicle_count
