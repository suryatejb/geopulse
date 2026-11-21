import asyncio
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

import app.main as api_module
from app.main import create_app


class FakeCursor:
    def __init__(self, row: dict[str, Any] | None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self.row


class FakeConnection:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    def execute(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> FakeCursor:
        if query.strip().startswith("INSERT INTO pings"):
            assert parameters is not None
            if parameters[1] is not None and any(
                ping["vehicle_id"] == parameters[0]
                and ping["sequence_number"] == parameters[1]
                for ping in self.pool.pings
            ):
                return FakeCursor(None)
            row = {
                "id": len(self.pool.pings) + 1,
                "vehicle_id": parameters[0],
                "sequence_number": parameters[1],
                "latitude": parameters[2],
                "longitude": parameters[3],
                "speed_kmh": parameters[4],
                "recorded_at": parameters[5],
                "received_at": datetime.now(timezone.utc),
            }
            self.pool.pings.append(row)
            return FakeCursor(row)
        if query.strip().startswith("SELECT id, vehicle_id"):
            assert parameters is not None
            return FakeCursor(next(
                (ping for ping in self.pool.pings
                 if ping["vehicle_id"] == parameters[0]
                 and ping["sequence_number"] == parameters[1]),
                None,
            ))
        if "COUNT(*)" in query:
            return FakeCursor({"count": len(self.pool.pings)})
        return FakeCursor({"result": 1})


class FakePool:
    def __init__(self) -> None:
        self.pings: list[dict[str, Any]] = []

    @contextmanager
    def connection(self):
        yield FakeConnection(self)


class FakeProducer:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def send_and_wait(self, topic: str, *, key: bytes, value: bytes) -> None:
        self.messages.append({"topic": topic, "key": key, "value": value})


def test_ping_api_saves_valid_request_and_reports_count() -> None:
    app = create_app()
    app.state.pool = FakePool()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00001",
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "speed_kmh": 32.5,
                },
            )
            assert response.status_code == 201
            assert response.json()["id"] == 1
            assert response.json()["vehicle_id"] == "vehicle-00001"
            server_timing = response.headers["Server-Timing"]
            assert "db_pool;dur=" in server_timing
            assert "db_execute;dur=" in server_timing
            assert "db_transaction;dur=" in server_timing
            assert "api;dur=" in server_timing

            count_response = await client.get("/api/v1/pings/count")
            assert count_response.status_code == 200
            assert count_response.json() == {"count": 1}

            livez_response = await client.get("/livez")
            assert livez_response.status_code == 200
            assert livez_response.json() == {"status": "ok"}

            invalid_response = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00001",
                    "sequence_number": 2,
                    "latitude": 100,
                    "longitude": -74.006,
                    "speed_kmh": 32.5,
                },
            )
            assert invalid_response.status_code == 422

    asyncio.run(exercise_api())


def test_postgres_duplicate_sequence_returns_original_ping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PING_INGESTION_MODE", "postgres")
    app = create_app()
    app.state.pool = FakePool()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            ping = {
                "vehicle_id": "vehicle-00001",
                "sequence_number": 1,
                "latitude": 40.7128,
                "longitude": -74.006,
                "speed_kmh": 32.5,
            }
            first = await client.post("/api/v1/pings", json=ping)
            retry = await client.post("/api/v1/pings", json=ping)
            assert first.status_code == 201
            assert retry.status_code == 200
            assert retry.json()["id"] == first.json()["id"]
            assert len(app.state.pool.pings) == 1

    asyncio.run(exercise_api())


def test_kafka_mode_returns_success_only_after_publishing_ping(
    monkeypatch,
) -> None:
    monkeypatch.setenv("PING_INGESTION_MODE", "kafka")
    app = create_app()
    app.state.pool = FakePool()
    app.state.kafka_producer = FakeProducer()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00042",
                    "sequence_number": 7,
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "speed_kmh": 32.5,
                },
            )
            missing_sequence = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00042",
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "speed_kmh": 32.5,
                },
            )

        assert response.status_code == 202
        assert missing_sequence.status_code == 422
        assert response.json()["vehicle_id"] == "vehicle-00042"
        assert response.json()["sequence_number"] == 7
        assert "kafka_send;dur=" in response.headers["Server-Timing"]
        assert len(app.state.kafka_producer.messages) == 1
        message = app.state.kafka_producer.messages[0]
        assert message["topic"] == "pings"
        assert message["key"] == b"vehicle-00042"
        assert '"sequence_number":7' in message["value"].decode("utf-8")
        assert app.state.pool.pings == []

    asyncio.run(exercise_api())


def test_kafka_mode_reports_unacknowledged_ping_as_unavailable(monkeypatch) -> None:
    monkeypatch.setenv("PING_INGESTION_MODE", "kafka")
    app = create_app()
    app.state.pool = FakePool()

    class FailedProducer:
        async def send_and_wait(self, topic: str, *, key: bytes, value: bytes) -> None:
            raise TimeoutError("broker did not acknowledge")

    app.state.kafka_producer = FailedProducer()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00042",
                    "sequence_number": 7,
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "speed_kmh": 32.5,
                },
            )
        assert response.status_code == 503

    asyncio.run(exercise_api())


def test_kafka_timeout_reports_unknown_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PING_INGESTION_MODE", "kafka")
    monkeypatch.setattr(api_module, "KAFKA_PUBLISH_TIMEOUT_SECONDS", 0.05)
    app = create_app()

    class SlowProducer:
        async def send_and_wait(self, topic: str, *, key: bytes, value: bytes) -> None:
            await asyncio.sleep(10)

    app.state.kafka_producer = SlowProducer()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            response = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00042",
                    "sequence_number": 7,
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "speed_kmh": 32.5,
                },
            )
            assert time.monotonic() - started < 1
            assert response.status_code == 503
            assert "may still" in response.json()["detail"]

    asyncio.run(exercise_api())


def test_kafka_response_waits_for_acknowledgment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PING_INGESTION_MODE", "kafka")
    app = create_app()

    async def exercise_api() -> None:
        started = asyncio.Event()
        acknowledged = asyncio.Event()

        class DelayedProducer:
            async def send_and_wait(self, topic: str, *, key: bytes, value: bytes) -> None:
                started.set()
                await acknowledged.wait()

        app.state.kafka_producer = DelayedProducer()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            request = asyncio.create_task(
                client.post(
                    "/api/v1/pings",
                    json={
                        "vehicle_id": "vehicle-00042",
                        "sequence_number": 1,
                        "latitude": 40.7128,
                        "longitude": -74.006,
                        "speed_kmh": 32.5,
                    },
                )
            )
            await asyncio.wait_for(started.wait(), timeout=1)
            assert not request.done()
            acknowledged.set()
            assert (await request).status_code == 202

    asyncio.run(exercise_api())


def test_slow_postgres_write_does_not_block_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PING_INGESTION_MODE", "postgres")
    app = create_app()

    class SlowPool(FakePool):
        @contextmanager
        def connection(self):
            time.sleep(0.3)
            yield FakeConnection(self)

    app.state.pool = SlowPool()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            started = time.monotonic()
            request = asyncio.create_task(
                client.post(
                    "/api/v1/pings",
                    json={
                        "vehicle_id": "vehicle-00001",
                        "sequence_number": 1,
                        "latitude": 40.7128,
                        "longitude": -74.006,
                        "speed_kmh": 32.5,
                    },
                )
            )
            await asyncio.sleep(0.05)
            response = await client.get("/livez")
            elapsed = time.monotonic() - started
            await request
            assert response.status_code == 200
            assert elapsed < 0.25

    asyncio.run(exercise_api())
