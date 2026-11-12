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


def test_latency_percentile_uses_nearest_rank() -> None:
    assert latency_percentile([40, 10, 30, 20], 95) == 40
    assert latency_percentile([40, 10, 30, 20], 50) == 20


def test_latency_percentile_rejects_empty_measurements() -> None:
    with pytest.raises(ValueError, match="at least one"):
        latency_percentile([], 95)


def test_latency_percentile_rejects_out_of_range_percentile() -> None:
    with pytest.raises(ValueError, match="between 0 and 100"):
        latency_percentile([10], 101)


def test_send_ping_returns_request_latency(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeResponse:
        status = 201

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"id": 1}'

    monkeypatch.setattr(
        vehicle_simulator,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(),
    )

    latency = vehicle_simulator.send_ping("http://example.test/pings", 1)

    assert latency >= 0
