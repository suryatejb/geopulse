import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import httpx
import pytest

import simulator.vehicle_simulator as vehicle_simulator
from simulator.vehicle_simulator import latency_percentile, make_ping


def test_simulator_generates_a_location_for_the_requested_vehicle() -> None:
    ping = make_ping(7, 3)

    assert ping["vehicle_id"] == "vehicle-00007"
    assert ping["sequence_number"] == 3
    assert 40.700 <= ping["latitude"] <= 40.800
    assert -74.020 <= ping["longitude"] <= -73.900
    assert 0 <= ping["speed_kmh"] <= 80
    assert isinstance(ping["recorded_at"], str)


def test_sequence_numbers_persist_and_remain_unique_across_runs(tmp_path: Path) -> None:
    path = tmp_path / "sequences.sqlite3"
    with closing(vehicle_simulator.open_sequence_store(path)) as store:
        assert vehicle_simulator.allocate_sequences(store, 2) == [1, 1]
        assert vehicle_simulator.allocate_sequences(store, 1) == [2]
    with closing(vehicle_simulator.open_sequence_store(path)) as store:
        assert vehicle_simulator.allocate_sequences(store, 3) == [3, 2, 1]


def test_concurrent_sequence_allocations_do_not_reuse_numbers(tmp_path: Path) -> None:
    path = tmp_path / "sequences.sqlite3"

    def allocate() -> list[int]:
        with closing(vehicle_simulator.open_sequence_store(path)) as store:
            return vehicle_simulator.allocate_sequences(store, 2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: allocate(), range(2)))

    assert sorted(results) == [[1, 1], [2, 2]]


def test_parse_server_timing_reads_known_measurements() -> None:
    timings = vehicle_simulator.parse_server_timing(
        "kafka_send;dur=2.500, api;dur=4.000"
    )

    assert timings == {
        "kafka_send": 2.5,
        "api": 4.0,
    }


def test_parse_server_timing_returns_empty_for_missing_header() -> None:
    assert vehicle_simulator.parse_server_timing(None) == {}


def test_parse_args_accepts_custom_worker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        vehicle_simulator.sys,
        "argv",
        ["vehicle_simulator", "--workers", "96"],
    )

    assert vehicle_simulator.parse_args().workers == 96


@pytest.mark.parametrize("worker_count", [0, -1])
def test_parse_args_rejects_non_positive_worker_count(
    monkeypatch: pytest.MonkeyPatch,
    worker_count: int,
) -> None:
    monkeypatch.setattr(
        vehicle_simulator.sys,
        "argv",
        ["vehicle_simulator", "--workers", str(worker_count)],
    )

    with pytest.raises(SystemExit):
        vehicle_simulator.parse_args()


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
            202,
            headers={
                "Server-Timing": (
                    "kafka_send;dur=2.500, api;dur=4.000, "
                    "db_pool;dur=1.000, db_execute;dur=1.500, "
                    "db_transaction;dur=2.000"
                )
            },
            request=request,
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        latency = vehicle_simulator.send_ping(
            client,
            "http://example.test/pings",
            1,
            1,
            time.perf_counter() - 0.001,
        )

    assert latency.client_ms >= 0
    assert latency.worker_queue_ms >= 0
    assert latency.api_ms == 4.0
    assert latency.kafka_send_ms == 2.5
    assert latency.db_pool_ms == 1.0
    assert latency.db_execute_ms == 1.5
    assert latency.db_transaction_ms == 2.0
