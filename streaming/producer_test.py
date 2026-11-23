import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from aiokafka import AIOKafkaProducer


async def main() -> None:
    bootstrap_servers = os.getenv(
        "KAFKA_BOOTSTRAP_SERVERS",
        "geopulse-kafka-bootstrap.kafka.svc:9092",
    )
    topic = os.getenv("KAFKA_TOPIC", "pings-phase3-live")
    producer = AIOKafkaProducer(bootstrap_servers=bootstrap_servers)
    await producer.start()
    try:
        now = datetime.now(timezone.utc)
        minute_start = int(os.getenv("PHASE3_TEST_MINUTE_START", "0"))
        if not minute_start:
            minute_start = int((now - timedelta(minutes=2)).timestamp()) // 60 * 60
        minute = datetime.fromtimestamp(minute_start, timezone.utc)
        pings = [
            ("phase3-a", 1, minute + timedelta(seconds=10), 20.0),
            ("phase3-a", 2, minute + timedelta(seconds=20), 60.0),
            ("phase3-b", 1, minute + timedelta(seconds=30), 10.0),
            ("phase3-watermark", 1, max(now, minute + timedelta(minutes=3)), 0.0),
        ]
        for vehicle_id, sequence_number, recorded_at, speed_kmh in pings:
            ping = {
                "vehicle_id": vehicle_id,
                "sequence_number": sequence_number,
                "latitude": 40.7128,
                "longitude": -74.006,
                "speed_kmh": speed_kmh,
                "recorded_at": recorded_at.isoformat().replace("+00:00", "Z"),
            }
            await producer.send_and_wait(topic, json.dumps(ping).encode("utf-8"))
        print(f"TARGET_MINUTE_START={minute_start}")
    finally:
        await producer.stop()


asyncio.run(main())
