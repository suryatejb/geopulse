import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiokafka import TopicPartition
from aiokafka.structs import OffsetAndMetadata

from app.kafka_drain import remaining_offsets, wait_for_drain


def test_remaining_offsets_counts_uncommitted_records() -> None:
    first = TopicPartition("pings", 0)
    second = TopicPartition("pings", 1)
    assert remaining_offsets(
        {first: 2, second: 0}, {first: 5, second: 3}, {first: 4}
    ) == 4


@pytest.mark.parametrize("committed", [-1, 2, 8])
def test_remaining_offsets_rejects_missing_expired_or_future_offsets(committed: int) -> None:
    partition = TopicPartition("pings", 0)
    with pytest.raises(RuntimeError):
        remaining_offsets({partition: 3}, {partition: 7}, {partition: committed})


def test_remaining_offsets_allows_new_empty_partition() -> None:
    partition = TopicPartition("pings", 0)
    assert remaining_offsets({partition: 0}, {partition: 0}, {}) == 0


@pytest.mark.parametrize("committed,expect_timeout", [(2, False), (1, True)])
def test_drain_checks_group_offsets_without_joining(
    monkeypatch: pytest.MonkeyPatch, committed: int, expect_timeout: bool
) -> None:
    partition = TopicPartition("pings", 0)
    consumer = MagicMock()
    consumer.start = AsyncMock()
    consumer.stop = AsyncMock()
    consumer.partitions_for_topic.return_value = {0}
    consumer.beginning_offsets = AsyncMock(return_value={partition: 0})
    consumer.end_offsets = AsyncMock(return_value={partition: 2})
    admin = MagicMock()
    admin.start = AsyncMock()
    admin.close = AsyncMock()
    admin.list_consumer_group_offsets = AsyncMock(
        return_value={partition: OffsetAndMetadata(committed, "")}
    )
    consumer_factory = MagicMock(return_value=consumer)
    monkeypatch.delenv("KAFKA_TOPIC", raising=False)
    monkeypatch.delenv("KAFKA_GROUP_ID", raising=False)
    monkeypatch.setattr("app.kafka_drain.AIOKafkaConsumer", consumer_factory)
    monkeypatch.setattr("app.kafka_drain.AIOKafkaAdminClient", lambda **kwargs: admin)

    if expect_timeout:
        with pytest.raises(TimeoutError):
            asyncio.run(wait_for_drain(0))
    else:
        asyncio.run(wait_for_drain(0))

    assert "group_id" not in consumer_factory.call_args.kwargs
    admin.list_consumer_group_offsets.assert_awaited_once_with(
        "geopulse-ping-writer", partitions=[partition]
    )
    admin.close.assert_awaited_once()
    consumer.stop.assert_awaited_once()


def test_drain_rejects_missing_partition_offsets(monkeypatch: pytest.MonkeyPatch) -> None:
    consumer = MagicMock()
    consumer.start = AsyncMock()
    consumer.stop = AsyncMock()
    consumer.partitions_for_topic.return_value = {0, 1}
    consumer.beginning_offsets = AsyncMock(return_value={TopicPartition("pings", 0): 0})
    consumer.end_offsets = AsyncMock(return_value={TopicPartition("pings", 0): 0})
    admin = MagicMock()
    admin.start = AsyncMock()
    admin.close = AsyncMock()
    monkeypatch.delenv("KAFKA_TOPIC", raising=False)
    monkeypatch.setattr("app.kafka_drain.AIOKafkaConsumer", lambda *args, **kwargs: consumer)
    monkeypatch.setattr("app.kafka_drain.AIOKafkaAdminClient", lambda **kwargs: admin)

    with pytest.raises(RuntimeError, match="every partition"):
        asyncio.run(wait_for_drain(0))
    admin.close.assert_awaited_once()
    consumer.stop.assert_awaited_once()
