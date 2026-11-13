import argparse
import math
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import httpx


def make_ping(vehicle_number: int) -> dict[str, str | float]:
    return {
        "vehicle_id": f"vehicle-{vehicle_number:05d}",
        "latitude": round(random.uniform(40.700, 40.800), 6),
        "longitude": round(random.uniform(-74.020, -73.900), 6),
        "speed_kmh": round(random.uniform(0, 80), 1),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def send_ping(client: httpx.Client, api_url: str, vehicle_number: int) -> float:
    started_at = time.perf_counter()
    response = client.post(api_url, json=make_ping(vehicle_number))
    if response.status_code != 201:
        raise RuntimeError(f"API returned unexpected status {response.status_code}")
    return (time.perf_counter() - started_at) * 1000


def latency_percentile(latencies: list[float], percentile: float) -> float:
    if not latencies:
        raise ValueError("latencies must contain at least one measurement")
    if not 0 <= percentile <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered_latencies = sorted(latencies)
    index = max(0, math.ceil(percentile / 100 * len(ordered_latencies)) - 1)
    return ordered_latencies[index]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one simulated GPS ping per vehicle each second."
    )
    parser.add_argument("--vehicles", type=int, default=100)
    parser.add_argument("--duration", type=int, default=10, help="Run time in seconds")
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000/api/v1/pings",
    )
    args = parser.parse_args()
    if args.vehicles < 1:
        parser.error("--vehicles must be at least 1")
    if args.duration < 1:
        parser.error("--duration must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    successes = 0
    latencies: list[float] = []
    failures: list[str] = []
    started_at = time.perf_counter()

    limits = httpx.Limits(max_connections=64, max_keepalive_connections=64)
    with httpx.Client(timeout=10, limits=limits) as client:
        with ThreadPoolExecutor(max_workers=min(args.vehicles, 64)) as executor:
            for _ in range(args.duration):
                tick_started_at = time.perf_counter()
                futures = [
                    executor.submit(send_ping, client, args.api_url, vehicle_number)
                    for vehicle_number in range(args.vehicles)
                ]
                for future in futures:
                    try:
                        latencies.append(future.result())
                        successes += 1
                    except (httpx.RequestError, RuntimeError) as error:
                        if len(failures) < 3:
                            failures.append(str(error))
                remaining_tick_time = 1 - (time.perf_counter() - tick_started_at)
                if remaining_tick_time > 0:
                    time.sleep(remaining_tick_time)

    elapsed = time.perf_counter() - started_at
    print(f"Expected pings: {args.vehicles * args.duration}")
    print(f"Saved pings:   {successes}")
    print(f"Failed pings:  {args.vehicles * args.duration - successes}")
    print(f"Elapsed:       {elapsed:.2f} seconds")
    print(f"Throughput:    {successes / elapsed:.1f} pings/second")
    if latencies:
        median_latency = statistics.median(latencies)
        p95_latency = latency_percentile(latencies, 95)
        print(
            "Latency:       "
            f"median {median_latency:.1f} ms, "
            f"p95 {p95_latency:.1f} ms (successful requests)"
        )
    else:
        print("Latency:       unavailable (no successful requests)")
    for failure in failures:
        print(f"Example failure: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
