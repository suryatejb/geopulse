import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import httpx

from app.main import create_app


class FakeCursor:
    def __init__(self, row: dict[str, Any]) -> None:
        self.row = row

    def fetchone(self) -> dict[str, Any]:
        return self.row


class FakeConnection:
    def __init__(self, pool: "FakePool") -> None:
        self.pool = pool

    def execute(
        self, query: str, parameters: tuple[Any, ...] | None = None
    ) -> FakeCursor:
        if query.strip().startswith("INSERT INTO pings"):
            assert parameters is not None
            row = {
                "id": len(self.pool.pings) + 1,
                "vehicle_id": parameters[0],
                "latitude": parameters[1],
                "longitude": parameters[2],
                "speed_kmh": parameters[3],
                "recorded_at": parameters[4],
                "received_at": datetime.now(timezone.utc),
            }
            self.pool.pings.append(row)
            return FakeCursor(row)
        if "COUNT(*)" in query:
            return FakeCursor({"count": len(self.pool.pings)})
        return FakeCursor({"result": 1})


class FakePool:
    def __init__(self) -> None:
        self.pings: list[dict[str, Any]] = []

    @contextmanager
    def connection(self):
        yield FakeConnection(self)


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

            invalid_response = await client.post(
                "/api/v1/pings",
                json={
                    "vehicle_id": "vehicle-00001",
                    "latitude": 100,
                    "longitude": -74.006,
                    "speed_kmh": 32.5,
                },
            )
            assert invalid_response.status_code == 422

    asyncio.run(exercise_api())
