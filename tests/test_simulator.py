import time

import httpx
import pytest

import simulator.vehicle_simulator as vehicle_simulator
from simulator.vehicle_simulator import latency_percentile, make_ping


def test_simulator_generates_a_location_for_the_requested_vehicle() -> None:
    ping = make_ping(7)

    assert ping["vehicle_id"] == "vehicle-00007"
    assert 40.700 <= ping["latitude"] <= 40.800
    assert -74.020 <= ping["longitude"] <= -73.900
    assert 0 <= ping["speed_kmh"] <= 80
    assert isinstance(ping["recorded_at"], str)


def test_parse_server_timing_reads_known_measurements() -> None:
    timings = vehicle_simulator.parse_server_timing(
        "db_pool;dur=1.250, db_execute;dur=2.500, api;dur=4.000"
    )

    assert timings == {
        "db_pool": 1.25,
        "db_execute": 2.5,
        "api": 4.0,
    }


def test_parse_server_timing_returns_empty_for_missing_header() -> None:
    assert vehicle_simulator.parse_server_timing(None) == {}


def test_latency_percentile_uses_nearest_rank() -> None:
    assert latency_percentile([40, 10, 30, 20], 95) == 40
    assert latency_percentile([40, 10, 30, 20], 50) == 20


def test_latency_percentile_rejects_empty_measurements() -> None:
    with pytest.raises(ValueError, match="at least one"):
        latency_percentile([], 95)


def test_latency_percentile_rejects_out_of_range_percentile() -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        latency_percentile([10], 101)


def test_send_ping_returns_request_latency() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://example.test/pings"
        return httpx.Response(
            201,
            headers={
                "Server-Timing": (
                    "db_pool;dur=1.250, db_execute;dur=2.500, "
                    "db_transaction;dur=3.000, api;dur=4.000"
                )
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        latency = vehicle_simulator.send_ping(
            client,
            "http://example.test/pings",
            1,
            time.perf_counter() - 0.001,
        )

    assert latency.client_ms >= 0
    assert latency.worker_queue_ms >= 0
    assert latency.api_ms == 4.0
    assert latency.db_pool_ms == 1.25
    assert latency.db_execute_ms == 2.5
    assert latency.db_transaction_ms == 3.0
