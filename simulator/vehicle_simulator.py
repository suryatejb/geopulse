import argparse
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def make_ping(vehicle_number: int) -> dict[str, str | float]:
    return {
        "vehicle_id": f"vehicle-{vehicle_number:05d}",
        "latitude": round(random.uniform(40.700, 40.800), 6),
        "longitude": round(random.uniform(-74.020, -73.900), 6),
        "speed_kmh": round(random.uniform(0, 80), 1),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def send_ping(api_url: str, vehicle_number: int) -> None:
    payload = json.dumps(make_ping(vehicle_number)).encode("utf-8")
    request = Request(
        api_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:
        if response.status != 201:
            raise RuntimeError(f"API returned unexpected status {response.status}")


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
    failures: list[str] = []
    started_at = time.perf_counter()

    with ThreadPoolExecutor(max_workers=min(args.vehicles, 64)) as executor:
        for _ in range(args.duration):
            tick_started_at = time.perf_counter()
            futures = [
                executor.submit(send_ping, args.api_url, vehicle_number)
                for vehicle_number in range(args.vehicles)
            ]
            for future in futures:
                try:
                    future.result()
                    successes += 1
                except (HTTPError, URLError, TimeoutError, RuntimeError) as error:
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
    for failure in failures:
        print(f"Example failure: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
