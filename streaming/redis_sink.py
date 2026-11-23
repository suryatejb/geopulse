import math

from redis import Redis

from app.area_stats import read_area_stats, stats_key

__all__ = [
    "RETENTION_SECONDS",
    "UPDATE_VEHICLE",
    "read_area_stats",
    "stats_key",
    "store_vehicle_speed",
]

RETENTION_SECONDS = 24 * 60 * 60

UPDATE_VEHICLE = """
local function compare_integer(a, b)
    if a == b then return 0 end
    local aneg = string.sub(a, 1, 1) == "-"
    local bneg = string.sub(b, 1, 1) == "-"
    if aneg ~= bneg then return aneg and -1 or 1 end
    if aneg then a = string.sub(a, 2) end
    if bneg then b = string.sub(b, 2) end
    local result
    if #a ~= #b then
        result = #a < #b and -1 or 1
    else
        result = a < b and -1 or 1
    end
    return aneg and -result or result
end

local expires_at = tonumber(ARGV[5])
if tonumber(redis.call("TIME")[1]) >= expires_at then return 0 end

local field = "vehicle:" .. ARGV[1]
local previous = redis.call("HGET", KEYS[1], field)
local speed = tonumber(ARGV[4])
if previous then
    local old = cjson.decode(previous)
    local comparison = compare_integer(ARGV[2], old[1])
    if comparison == 0 then comparison = compare_integer(ARGV[3], old[2]) end
    if comparison == 0 then
        if speed <= tonumber(old[3]) then return 0 end
    elseif comparison < 0 then
        return 0
    end
    redis.call("HINCRBYFLOAT", KEYS[1], "speed_sum", speed - tonumber(old[3]))
else
    redis.call("HINCRBY", KEYS[1], "vehicle_count", 1)
    redis.call("HINCRBYFLOAT", KEYS[1], "speed_sum", speed)
end
redis.call("HSET", KEYS[1], field, cjson.encode({ARGV[2], ARGV[3], ARGV[4]}))
redis.call("EXPIREAT", KEYS[1], expires_at)
return 1
"""


def store_vehicle_speed(
    client: Redis,
    *,
    cell: str,
    minute_start: int,
    vehicle_id: str,
    recorded_at_us: int,
    sequence_number: int,
    speed_kmh: float,
) -> bool:
    key = stats_key(cell, minute_start)
    if not minute_start * 1_000_000 <= recorded_at_us < (minute_start + 60) * 1_000_000:
        raise ValueError("recorded_at_us must fall within the minute")
    if not 1 <= len(vehicle_id) <= 100:
        raise ValueError("vehicle_id must contain 1 to 100 characters")
    if not 1 <= sequence_number <= 2**63 - 1:
        raise ValueError("sequence_number is outside the PostgreSQL BIGINT range")
    if not math.isfinite(speed_kmh) or not 0 <= speed_kmh <= 300:
        raise ValueError("speed_kmh must be finite and between 0 and 300")

    result = client.eval(
        UPDATE_VEHICLE,
        1,
        key,
        vehicle_id,
        str(recorded_at_us),
        str(sequence_number),
        str(speed_kmh),
        minute_start + 60 + RETENTION_SECONDS,
    )
    return result == 1
