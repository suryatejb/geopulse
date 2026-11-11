from simulator.vehicle_simulator import make_ping


def test_simulator_generates_a_location_for_the_requested_vehicle() -> None:
    ping = make_ping(7)

    assert ping["vehicle_id"] == "vehicle-00007"
    assert 40.700 <= ping["latitude"] <= 40.800
    assert -74.020 <= ping["longitude"] <= -73.900
    assert 0 <= ping["speed_kmh"] <= 80
    assert isinstance(ping["recorded_at"], str)
