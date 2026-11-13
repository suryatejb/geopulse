# GeoPulse

GeoPulse is a learning project: we build a small real-time vehicle-location
system, measure its limits, and then improve it one phase at a time.

## Phase 1: the baseline

**Goal:** make simulated vehicles send GPS pings to a web API, save each ping
straight into PostgreSQL, and measure how the simple design behaves.

The API writes each request to the database before replying. This is easy to
understand, but it means every vehicle request waits for a database write.
Later, Phase 2 will add Kafka as a buffer. For now, the direct write gives us a
baseline to compare against.

### What is in this phase

- `app/main.py` — the FastAPI service and its database reads/writes.
- `app/models.py` — validation for each incoming GPS ping.
- `simulator/vehicle_simulator.py` — sends one ping per vehicle per second.
- `tests/` — checks the input rules without needing a running database.

The simulator currently generates sample coordinates inside a small New York
area. It does not follow actual roads yet; OpenStreetMap road routes can come
later.

## Set up on Windows

This phase uses Python 3.11+ and a local PostgreSQL server. Docker is not
required.

1. In pgAdmin, connect to your local PostgreSQL server and open the Query Tool.
   Create a project-specific login and database. Replace the sample password
   with one you choose locally, and do not share it. Run the statements
   separately: PostgreSQL does not allow `CREATE DATABASE` inside a transaction.

   ```sql
   CREATE ROLE geopulse WITH LOGIN PASSWORD 'choose-a-local-password';
   ```

   After that succeeds, run:

   ```sql
   CREATE DATABASE geopulse OWNER geopulse;
   ```

2. In PowerShell, from the project folder, make a virtual environment and
   install the packages:

   ```powershell
   py -3.11 -m venv .venv
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env`. Edit `.env` and replace the example password
   in `DATABASE_URL` with the password you chose. Keep `.env` private; Git
   ignores it.

4. Start the API:

   ```powershell
   python -m uvicorn app.main:app --reload
   ```

   The first start creates the `pings` table and its vehicle/time index. Open
   <http://127.0.0.1:8000/docs> to try the API, or
   <http://127.0.0.1:8000/health> to check that the database is reachable.

5. In a second PowerShell window, activate the same virtual environment and run
   a small load test:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python -m simulator.vehicle_simulator --vehicles 100 --duration 10
   ```

   The simulator prints expected and saved ping counts, throughput, and median
   and 95th-percentile timings for successful requests. It measures:

   - **Client latency:** the HTTP request and response, excluding time waiting
     for a simulator worker.
   - **Simulator worker queue:** time waiting for a worker to start the request.
   - **API time:** time spent handling the request inside FastAPI.
   - **DB connection-pool wait:** time waiting to acquire a PostgreSQL
     connection.
   - **DB execute:** time for the insert query and returned row.
   - **DB transaction:** time from acquiring the connection through finishing
     the transaction and returning the connection to the pool.

   The API reports its timings using the standard `Server-Timing` response
   header, which the simulator reads. The 95th percentile is the nearest-rank
   value: 95% of successful requests took that long or less. The simulator
   reuses HTTP connections between pings. Check the total stored count at
   <http://127.0.0.1:8000/api/v1/pings/count>.

   Increase the load gradually, for example to 250 and then 500 vehicles. Stop
   if your computer becomes unresponsive. Compare runs using the same duration;
   this is a learning benchmark, not a claim about production capacity.

6. Run the tests:

   ```powershell
   python -m pytest
   ```

### Baseline results (November 11, 2026)

These are observed results from one local Windows computer with Python 3.11 and
PostgreSQL 17. Each run completed with zero failed pings.

| Vehicles | Duration setting | Pings saved | Elapsed | Measured throughput |
| ---: | ---: | ---: | ---: | ---: |
| 10 | 3 s | 30 | 3.00 s | 10.0 pings/s |
| 100 | 10 s | 1,000 | 10.01 s | 99.9 pings/s |
| 250 | 10 s | 2,500 | 10.01 s | 249.8 pings/s |
| 500 | 10 s | 5,000 | 11.50 s | 434.6 pings/s |
| 500 (repeat) | 10 s | 5,000 | 12.10 s | 413.1 pings/s |
| 750 | 10 s | 7,500 | 16.66 s | 450.3 pings/s |

These earlier runs were made before the simulator recorded request latency, so
their median and p95 latency are not available.

### Latency-enabled repeat runs

The simulator was updated to record successful HTTP request latency. These
single local runs used the same Windows computer and PostgreSQL setup:

| Vehicles | Pings saved | Elapsed | Throughput | Median latency | p95 latency |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 500 | 5,000 | 12.47 s | 400.8 pings/s | 140.5 ms | 250.2 ms |
| 750 | 7,500 | 16.73 s | 448.2 pings/s | 138.4 ms | 163.9 ms |

Both runs had zero failed requests, and the database count increased by exactly
12,500 across the two runs. Latency starts when a worker begins the HTTP request
and ends after it reads the response body. It does not include time waiting in
the simulator's thread-pool queue before a worker starts the request. The
simulator now reuses HTTP connections; earlier latency results above were
collected before connection reuse was added and are not directly comparable.
Those initial latency measurements were single runs; their lower 750-vehicle
latency did not establish a trend. Updated repeat tests follow.

### Connection-reusing repeat tests (November 12, 2026)

The simulator now reuses HTTP connections. Across three 10-second runs at each
load:

| Vehicles | Run 1 saved | Run 2 saved | Run 3 saved | Failed requests |
| ---: | ---: | ---: | ---: | ---: |
| 500 | 5,000 | 5,000 | 5,000 | 0 total |
| 750 | 7,499 | 7,500 | 7,500 | 1 total |

The 750-vehicle run with one failure reported a connection reset. The database
count matched the successful-ping totals across these runs.

One additional run at each load gave these latency measurements:

| Vehicles | Pings saved | Elapsed | Throughput | Median latency | p95 latency |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 500 | 5,000 | 10.37 s | 482.1 pings/s | 107.8 ms | 212.0 ms |
| 750 | 7,493 | 60.54 s | 123.8 pings/s | 373.6 ms | 1,209.7 ms |

The additional 750-vehicle run had 7 failed requests; the database count
increased by exactly the 12,493 successful requests. These results vary
substantially between runs. They show that the current setup can experience
slow responses and connection failures at this load, but do not by themselves
identify whether the simulator, API, or database is the cause.

### Component timing diagnosis (November 13, 2026)

The API now reports `Server-Timing` measurements, and the simulator reports
time spent waiting for one of its request workers. These fresh 10-second runs
had zero failed requests:

| Vehicles | Saved | Throughput | Client median / p95 | Worker queue median / p95 | API median / p95 | DB pool wait median / p95 | DB execute median / p95 | DB transaction median / p95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 500 | 5,000 | 477.3/s | 91.8 / 215.4 ms | 383.0 / 883.7 ms | 2.0 / 56.8 ms | 0.0 / 0.0 ms | 0.3 / 2.0 ms | 0.6 / 2.8 ms |
| 750 | 7,500 | 512.4/s | 98.8 / 229.2 ms | 595.9 / 1,424.5 ms | 2.2 / 68.9 ms | 0.0 / 0.0 ms | 0.4 / 2.4 ms | 0.7 / 3.4 ms |

Pool-wait values are rounded to one decimal place. The database timings are
small in these runs, while the simulator worker queue is much larger and grows
at 750 vehicles. This points to the simulator's 64-worker load-generation limit
as a major factor in these measurements; it does not support blaming PostgreSQL
for the earlier slow runs. The API remained healthy, and the database count
increased from 12,485 to 24,985, exactly matching the 12,500 successful pings.
This is one fresh run at each load, so intermittent connection resets from
earlier tests are not explained or ruled out.

The simulator calculates throughput as successful pings divided by the entire
run time. In the original baseline tests, larger runs took longer than their
requested duration, and measured throughput rose only slightly from 500 to
750 vehicles. The connection-reusing repeats above also varied substantially.
These results do **not** identify whether the simulator, HTTP requests, API, or
database is the limiting component. The simulator currently uses at most 64
concurrent request workers, so its capacity is part of the measurement too.

## The seven phases

1. **Baseline:** send pings directly to the database and find the first
   bottleneck.
2. **Kafka:** buffer pings in a durable queue so ingestion does not wait for
   database writes.
3. **Live statistics:** process the stream into up-to-date area-level numbers.
4. **Sharding:** spread historical pings across database servers.
5. **Kubernetes:** run, restart, and scale services from a declarative setup.
6. **Reliability:** add monitoring and controlled failure tests.
7. **Batch analytics:** analyze the full history with Spark.

Each phase will build on measured results from the previous one; we will not
start every infrastructure tool at once.
