import asyncio
import json
import os
from datetime import datetime, timezone

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
        minute = now.replace(second=10, microsecond=0)
        pings = [
            ("phase3-a", 1, minute, 20.0),
            ("phase3-a", 2, minute.replace(second=20), 60.0),
            ("phase3-b", 1, minute.replace(second=30), 10.0),
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
    finally:
        await producer.stop()


asyncio.run(main())
