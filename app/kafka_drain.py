import asyncio
import os
import time

from aiokafka import AIOKafkaConsumer, TopicPartition
from aiokafka.admin import AIOKafkaAdminClient


def remaining_offsets(
    starts: dict[TopicPartition, int],
    ends: dict[TopicPartition, int],
    committed: dict[TopicPartition, int],
) -> int:
    remaining = 0
    for partition, end in ends.items():
        start = starts[partition]
        offset = committed.get(partition, -1)
        if offset < 0:
            if start != 0:
                raise RuntimeError(f"No committed offset for {partition}; earlier data may have expired.")
            offset = 0
        if offset < start or offset > end:
            raise RuntimeError(f"Committed offset for {partition} is outside the retained range.")
        remaining += end - offset
    return remaining


async def wait_for_drain(timeout_seconds: float) -> None:
    topic = os.getenv("KAFKA_TOPIC", "pings")
    group = os.getenv("KAFKA_GROUP_ID", "geopulse-ping-writer")
    servers = os.getenv("KAFKA_BOOTSTRAP_SERVERS", "geopulse-kafka-bootstrap.kafka.svc:9092")
    consumer = AIOKafkaConsumer(topic, bootstrap_servers=servers, enable_auto_commit=False)
    admin = AIOKafkaAdminClient(bootstrap_servers=servers)
    try:
        await consumer.start()
        await admin.start()
        deadline = time.monotonic() + timeout_seconds
        while True:
            partitions = consumer.partitions_for_topic(topic)
            if not partitions:
                raise RuntimeError(f"Could not find partitions for Kafka topic {topic!r}.")
            assigned = [TopicPartition(topic, p) for p in sorted(partitions)]
            starts = await consumer.beginning_offsets(assigned)
            ends = await consumer.end_offsets(assigned)
            if set(starts) != set(assigned) or set(ends) != set(assigned):
                raise RuntimeError("Kafka did not return offsets for every partition.")
            offsets = await admin.list_consumer_group_offsets(group, partitions=assigned)
            committed = {partition: value.offset for partition, value in offsets.items()}
            remaining = remaining_offsets(starts, ends, committed)
            print(f"Kafka writer has {remaining} offsets remaining.", flush=True)
            if remaining == 0:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Kafka writer did not drain before the deadline.")
            await asyncio.sleep(2)
    finally:
        await admin.close()
        await consumer.stop()


if __name__ == "__main__":
    asyncio.run(wait_for_drain(float(os.getenv("KAFKA_DRAIN_TIMEOUT_SECONDS", "180"))))
