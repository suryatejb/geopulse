import argparse
import math
import random
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx


@dataclass(frozen=True)
class RequestTiming:
    client_ms: float
    worker_queue_ms: float
    api_ms: float | None
    db_pool_ms: float | None
    db_execute_ms: float | None
    db_transaction_ms: float | None


def make_ping(vehicle_number: int) -> dict[str, str | float]:
    return {
        "vehicle_id": f"vehicle-{vehicle_number:05d}",
        "latitude": round(random.uniform(40.700, 40.800), 6),
        "longitude": round(random.uniform(-74.020, -73.900), 6),
        "speed_kmh": round(random.uniform(0, 80), 1),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_server_timing(header: str | None) -> dict[str, float]:
    timings: dict[str, float] = {}
    if not header:
        return timings

    for metric in header.split(","):
        name, *parameters = metric.strip().split(";")
        for parameter in parameters:
            key, separator, value = parameter.strip().partition("=")
            if key == "dur" and separator:
                timings[name.strip()] = float(value)

    return timings


def send_ping(
    client: httpx.Client,
    api_url: str,
    vehicle_number: int,
    submitted_at: float,
) -> RequestTiming:
    started_at = time.perf_counter()
    response = client.post(api_url, json=make_ping(vehicle_number))
    if response.status_code != 201:
        raise RuntimeError(f"API returned unexpected status {response.status_code}")
    request_ended_at = time.perf_counter()
    server_timings = parse_server_timing(response.headers.get("Server-Timing"))
    return RequestTiming(
        client_ms=(request_ended_at - started_at) * 1000,
        worker_queue_ms=(started_at - submitted_at) * 1000,
        api_ms=server_timings.get("api"),
        db_pool_ms=server_timings.get("db_pool"),
        db_execute_ms=server_timings.get("db_execute"),
        db_transaction_ms=server_timings.get("db_transaction"),
    )


def latency_percentile(latencies: list[float], percentile: float) -> float:
    if not latencies:
        raise ValueError("latencies must contain at least one measurement")
    if not 0 <= percentile <= 100:
        raise ValueError("percentile must be between 0 and 100")
    ordered_latencies = sorted(latencies)
    index = max(0, math.ceil(percentile / 100 * len(ordered_latencies)) - 1)
    return ordered_latencies[index]


def print_timing_summary(label: str, timings: list[float | None]) -> None:
    available_timings = [timing for timing in timings if timing is not None]
    if not available_timings:
        print(f"{label}: unavailable")
        return

    median_latency = statistics.median(available_timings)
    p95_latency = latency_percentile(available_timings, 95)
    print(
        f"{label}: median {median_latency:.1f} ms, "
        f"p95 {p95_latency:.1f} ms (n={len(available_timings)})"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one simulated GPS ping per vehicle each second."
    )
    parser.add_argument("--vehicles", type=int, default=100)
    parser.add_argument("--duration", type=int, default=10, help="Run time in seconds")
    parser.add_argument(
        "--workers",
        type=int,
        default=64,
        help="Maximum concurrent simulator requests",
    )
    parser.add_argument(
        "--api-url",
        default="http://127.0.0.1:8000/api/v1/pings",
    )
    args = parser.parse_args()
    if args.vehicles < 1:
        parser.error("--vehicles must be at least 1")
    if args.duration < 1:
        parser.error("--duration must be at least 1")
    if args.workers < 1:
        parser.error("--workers must be at least 1")
    return args


def main() -> int:
    args = parse_args()
    worker_count = min(args.vehicles, args.workers)
    timings: list[RequestTiming] = []
    failures: list[str] = []
    started_at = time.perf_counter()

    limits = httpx.Limits(
        max_connections=worker_count,
        max_keepalive_connections=worker_count,
    )
    with httpx.Client(timeout=10, limits=limits) as client:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            for _ in range(args.duration):
                tick_started_at = time.perf_counter()
                futures = []
                for vehicle_number in range(args.vehicles):
                    submitted_at = time.perf_counter()
                    futures.append(
                        executor.submit(
                            send_ping,
                            client,
                            args.api_url,
                            vehicle_number,
                            submitted_at,
                        )
                    )
                for future in futures:
                    try:
                        timings.append(future.result())
                    except (httpx.RequestError, RuntimeError) as error:
                        if len(failures) < 3:
                            failures.append(str(error))
                remaining_tick_time = 1 - (time.perf_counter() - tick_started_at)
                if remaining_tick_time > 0:
                    time.sleep(remaining_tick_time)

    elapsed = time.perf_counter() - started_at
    successes = len(timings)
    print(f"Expected pings: {args.vehicles * args.duration}")
    print(f"Saved pings:   {successes}")
    print(f"Failed pings:  {args.vehicles * args.duration - successes}")
    print(f"Request workers: {worker_count}")
    print(f"Elapsed:       {elapsed:.2f} seconds")
    print(f"Throughput:    {successes / elapsed:.1f} pings/second")
    print_timing_summary("Client latency", [timing.client_ms for timing in timings])
    print_timing_summary(
        "Simulator worker queue",
        [timing.worker_queue_ms for timing in timings],
    )
    print_timing_summary("API time", [timing.api_ms for timing in timings])
    print_timing_summary("DB connection-pool wait", [timing.db_pool_ms for timing in timings])
    print_timing_summary("DB execute", [timing.db_execute_ms for timing in timings])
    print_timing_summary(
        "DB transaction",
        [timing.db_transaction_ms for timing in timings],
    )
    for failure in failures:
        print(f"Example failure: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
