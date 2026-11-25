import asyncio
import json
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import httpx
import pytest

import app.main as api_module
from app.main import create_app


class FakeCursor:
    def __init__(self, row: dict[str, Any] | list[dict[str, Any]] | None) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any] | None:
        return self.row if isinstance(self.row, dict) else None

    def fetchall(self) -> list[dict[str, Any]]:
        if isinstance(self.row, list):
            return self.row
        return []


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
        if "ORDER BY recorded_at DESC, id DESC" in query:
            assert parameters is not None
            vehicle_pings = sorted(
                (ping for ping in self.pool.pings if ping["vehicle_id"] == parameters[0]),
                key=lambda ping: (ping["recorded_at"], ping["id"]),
                reverse=True,
            )
            return FakeCursor(vehicle_pings[:parameters[1]])
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


class FakeStatsRedis:
    def __init__(self, values: list[bytes | None]) -> None:
        self.values = values
        self.requested_key: str | None = None

    def hmget(self, key: str, *fields: str) -> list[bytes | None]:
        self.requested_key = key
        assert fields == ("vehicle_count", "speed_sum")
        return self.values


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


def test_vehicle_pings_endpoint_returns_newest_records_with_a_limit() -> None:
    app = create_app()
    app.state.pool = FakePool()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            for sequence_number, second in [(1, 1), (2, 2), (3, 3)]:
                response = await client.post(
                    "/api/v1/pings",
                    json={
                        "vehicle_id": "vehicle-00042",
                        "sequence_number": sequence_number,
                        "latitude": 40.7128,
                        "longitude": -74.006,
                        "speed_kmh": 20 + sequence_number,
                        "recorded_at": f"2026-11-23T12:00:0{second}+00:00",
                    },
                )
                assert response.status_code == 201

            response = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00043",
                    "sequence_number": 1,
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "speed_kmh": 24,
                    "recorded_at": "2026-11-23T12:00:04+00:00",
                },
            )
            assert response.status_code == 201

            response = await client.get(
                "/api/v1/vehicles/vehicle-00042/pings",
                params={"limit": 2},
            )
            invalid_limit_response = await client.get(
                "/api/v1/vehicles/vehicle-00042/pings",
                params={"limit": 1001},
            )
        assert response.status_code == 200
        assert [ping["sequence_number"] for ping in response.json()] == [3, 2]
        assert invalid_limit_response.status_code == 422

    asyncio.run(exercise_api())


def test_area_stats_endpoint_returns_redis_average() -> None:
    app = create_app()
    app.state.redis = FakeStatsRedis([b"2", b"70"])

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get(
                "/api/v1/stats/area",
                params={
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "minute_start": 60,
                },
            )
        assert response.status_code == 200
        assert response.json() == {
            "h3_cell": "882a107289fffff",
            "minute_start": 60,
            "vehicle_count": 2,
            "average_speed_kmh": 35.0,
        }
        assert app.state.redis.requested_key == "geopulse:stats:60:882a107289fffff"

    asyncio.run(exercise_api())


def test_area_stats_endpoint_reports_missing_data_and_missing_redis() -> None:
    async def request_stats(app) -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            return await client.get(
                "/api/v1/stats/area",
                params={
                    "latitude": 40.7128,
                    "longitude": -74.006,
                    "minute_start": 60,
                },
            )

    async def exercise_api() -> None:
        no_redis = await request_stats(create_app())
        assert no_redis.status_code == 503

        no_data_app = create_app()
        no_data_app.state.redis = FakeStatsRedis([None, None])
        no_data = await request_stats(no_data_app)
        assert no_data.status_code == 404

    asyncio.run(exercise_api())


def test_dashboard_page_is_served() -> None:
    app = create_app()

    async def exercise_api() -> None:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            response = await client.get("/dashboard")
        assert response.status_code == 200
        assert "Live area statistics" in response.text

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


@pytest.mark.parametrize("topic", ["pings", "pings-isolated-test"])
def test_kafka_mode_returns_success_only_after_publishing_ping(
    monkeypatch, topic: str,
) -> None:
    monkeypatch.setenv("PING_INGESTION_MODE", "kafka")
    if topic != "pings":
        monkeypatch.setenv("KAFKA_TOPIC", topic)
    else:
        monkeypatch.delenv("KAFKA_TOPIC", raising=False)
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
        assert message["topic"] == topic
        assert message["key"] == b"vehicle-00042"
        payload = json.loads(message["value"])
        assert payload["sequence_number"] == 7
        assert payload["h3_cell"] == "882a107289fffff"
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
