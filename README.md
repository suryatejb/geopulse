# GeoPulse

GeoPulse is a learning project: we build a small real-time vehicle-location
system, measure its limits, and then improve it one phase at a time.

## Phase 0: the Kubernetes playground

The project plan starts with a local three-node Kubernetes cluster and
monitoring. The cluster uses kind: each Kubernetes node runs inside a Docker
container. One node is the control plane, which manages the cluster; two are
workers, which run applications.

On Windows, install and start Docker Desktop with the WSL 2 backend, and install
kind, kubectl, and Helm. From the project folder in PowerShell, create the
cluster using a project-specific kubeconfig rather than changing your default
Kubernetes context:

```powershell
New-Item -ItemType Directory -Force .geopulse | Out-Null
kind create cluster --name geopulse --config .\infra\kind\cluster.yaml --kubeconfig .\.geopulse\kubeconfig --wait 120s
$env:KUBECONFIG = (Resolve-Path .\.geopulse\kubeconfig).Path
kubectl get nodes
```

Set `KUBECONFIG` again when opening a new PowerShell window. The `.geopulse`
folder is ignored by Git because the kubeconfig contains cluster credentials.
The existing Phase 1 API and database still run locally; moving them into
Kubernetes is a separate step.

### Prometheus and Grafana

The local monitoring stack uses the `kube-prometheus-stack` Helm chart version
`93.3.0` and the settings in `infra/monitoring/values.yaml`. Prometheus collects
metrics, node exporters provide CPU and memory measurements for each node,
and Grafana displays them. Alertmanager is disabled for this resource-conscious
local setup; full alerting is part of Phase 6. Metrics use one-day retention
and are not configured for persistent storage.

To install or recreate monitoring, first set `KUBECONFIG` as above, then
run the following from the project folder. This creates a random Grafana
password only if the secret does not already exist. The password is sent to
Kubernetes over standard input, not put in a command-line argument.

```powershell
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Failed to create monitoring namespace.' }
$existing = kubectl -n monitoring get secret geopulse-grafana-admin --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) { throw 'Failed to check Grafana secret.' }
if (-not $existing) {
    $bytes = New-Object byte[] 24
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    $password = [Convert]::ToBase64String($bytes)
    $secret = @{
        apiVersion = 'v1'
        kind = 'Secret'
        metadata = @{ name = 'geopulse-grafana-admin'; namespace = 'monitoring' }
        type = 'Opaque'
        data = @{
            'admin-user' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('admin'))
            'admin-password' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($password))
        }
    } | ConvertTo-Json -Depth 5 -Compress
    $secret | kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'Failed to create Grafana secret.' }
    Remove-Variable bytes, password, secret
}

$helmRoot = Join-Path (Resolve-Path .\.geopulse).Path 'helm'
$env:HELM_REPOSITORY_CONFIG = Join-Path $helmRoot 'repositories.yaml'
$env:HELM_REPOSITORY_CACHE = Join-Path $helmRoot 'repository-cache'
New-Item -ItemType Directory -Force $env:HELM_REPOSITORY_CACHE | Out-Null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
if ($LASTEXITCODE -ne 0) { throw 'Failed to add Prometheus Helm repository.' }
helm repo update prometheus-community
if ($LASTEXITCODE -ne 0) { throw 'Failed to update Prometheus Helm repository.' }
helm upgrade --install geopulse-monitoring prometheus-community/kube-prometheus-stack --namespace monitoring --version 93.3.0 -f .\infra\monitoring\values.yaml --wait --timeout 12m
if ($LASTEXITCODE -ne 0) { throw 'Failed to install monitoring.' }
```

To open Grafana, run the following in a PowerShell window and leave it open:

```powershell
kubectl -n monitoring port-forward service/geopulse-monitoring-grafana 3000:80
```

Open <http://127.0.0.1:3000>. The username is `admin`. To see the password on
your own computer, use another PowerShell window with `KUBECONFIG` set:

```powershell
$secret = kubectl -n monitoring get secret geopulse-grafana-admin -o json | ConvertFrom-Json
[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($secret.data.'admin-password'))
```

Keep the password private. In Grafana, open **Dashboards** and search for
**Kubernetes / Compute Resources / Nodes Overview** or **Node Exporter / Nodes**
to see node CPU and memory. Stop port forwarding with Ctrl+C when finished.

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

   The simulator defaults to 64 concurrent request workers. You can change
   that limit for controlled comparisons, for example:

   ```powershell
   python -m simulator.vehicle_simulator --vehicles 750 --duration 10 --workers 96
   python -m simulator.vehicle_simulator --vehicles 750 --duration 10 --workers 128
   ```

   The actual worker count is the smaller of `--vehicles` and `--workers`.

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

### Simulator worker-count comparison (November 13, 2026)

The same vehicle loads were tested with 96 and 128 simulator request workers.
The earlier 64-worker measurements are included as a comparison:

| Workers | Vehicles | Saved | Failed | Elapsed | Throughput | Worker queue p95 | API p95 | DB pool wait p95 | DB transaction p95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 64 | 500 | 5,000 | 0 | ~10.5 s | 477.3/s | 883.7 ms | 56.8 ms | 0.0 ms | 2.8 ms |
| 64 | 750 | 7,500 | 0 | ~14.6 s | 512.4/s | 1,424.5 ms | 68.9 ms | 0.0 ms | 3.4 ms |
| 96 | 500 | 4,994 | 6 | 41.74 s | 119.6/s | 4,786.6 ms | 330.3 ms | 0.1 ms | 8.6 ms |
| 96 | 750 | 7,474 | 26 | 67.86 s | 110.1/s | 6,422.2 ms | 472.5 ms | 0.1 ms | 15.2 ms |
| 128 | 500 | 4,983 | 17 | 59.28 s | 84.1/s | 5,069.4 ms | 386.2 ms | 0.1 ms | 11.8 ms |
| 128 | 750 | 7,492 | 8 | 73.40 s | 102.1/s | 6,225.6 ms | 539.1 ms | 0.1 ms | 16.2 ms |

Increasing simulator workers made throughput worse and added request failures.
Meanwhile, database pool wait stayed at or below 0.1 ms, and the 95th-percentile
database transaction stayed at or below 16.2 ms. API time and simulator queue
time rose sharply. This points to API or local-machine overload rather than a
database bottleneck in these tests. The API stayed healthy and logged no
server-side errors; the client observed failed requests. The database row count
increased from 29,974 to 54,917, matching the 24,943 successful responses.

### Connection-reuse investigation (November 15, 2026)

A temporary tracer assigned each ping a unique ID and recorded when the API
received it. In a 96-worker, 500-vehicle, 10-round run, 28 requests failed with
Windows connection errors. None of those 28 IDs appeared at the application
entry point or in PostgreSQL. This narrows the observed failures to before the
application received them, but does not identify which component closed the
connections. Tracing adds overhead and may change timings.

An additional comparison alternated two connection settings. Each run used 96
workers, 500 vehicles, and five rounds (2,500 pings):

| Mode | Saved | Failed | Elapsed | Throughput | Distinct client address/port pairs seen by API |
| --- | ---: | ---: | ---: | ---: | ---: |
| Reuse | 2,493 | 7 | 8.47 s | 294.4/s | 161 |
| New connection per ping | 2,500 | 0 | 5.81 s | 430.4/s | 2,499 |
| Reuse | 2,500 | 0 | 8.56 s | 292.1/s | 167 |
| New connection per ping | 2,500 | 0 | 6.87 s | 364.0/s | 2,500 |

The seven failed IDs were not seen by the application or found in PostgreSQL;
the database count increase matched successful responses in each run. These
results make connection reuse a lead worth investigating, not proof of a root
cause: the second reuse run had no failures, and these are only two runs per
mode. Address/port pairs are not a precise count of TCP connections because
ports can be reused. Opening a new connection per ping can exhaust local
ports, so it is not established as a safe production fix. The comparison used
temporary tracing scripts rather than changing the project's simulator.

#### Client idle-expiry comparison

The same traced setup was run six times, alternating HTTPX's default 5-second
idle-connection expiry with a 1-second expiry. Uvicorn's idle timeout remained
5 seconds; each run used 96 workers, 500 vehicles, and five rounds.

| Client expiry | Saved | Failed | Elapsed | Throughput | Distinct client address/port pairs seen by API |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 5 s | 2,500 | 0 | 8.55 s | 292.6/s | 166 |
| 1 s | 2,500 | 0 | 7.07 s | 353.8/s | 345 |
| 5 s | 2,500 | 0 | 8.55 s | 292.4/s | 184 |
| 1 s | 2,500 | 0 | 7.06 s | 354.2/s | 354 |
| 5 s | 2,494 | 6 | 8.45 s | 295.2/s | 177 |
| 1 s | 2,500 | 0 | 7.03 s | 355.6/s | 339 |

The six failed requests were connection read errors; their IDs were not seen
at the application entry point or in PostgreSQL. Database count increases
matched the successful-response counts. The shorter expiry had no failures in
these three runs, but it used more observed address/port pairs; the sample is
too small to establish that it fixes the intermittent resets. Pair counts are
not exact connection counts because ports may be reused. Server-observed
response-to-next-request gaps are only a proxy for connection idleness and
cannot measure the idle age of the failed requests. The experiment used
temporary tracing scripts and did not change the simulator's default expiry.

The simulator calculates throughput as successful pings divided by the entire
run time. Taken together, these measurements do not indicate that PostgreSQL
is the bottleneck: its connection wait and transaction times remain low. Higher
simulator concurrency instead coincides with longer simulator queues, slower
API handling, and request failures. We still have not isolated the exact API
or local-machine resource involved, nor explained the earlier intermittent
connection resets. These local results are not a production capacity claim.

## The eight phases

0. **Setup:** create the Kubernetes playground and basic monitoring.
1. **Baseline:** send pings directly to the database and measure performance.
2. **Kafka:** buffer pings durably and save them using separate workers.
3. **Live statistics:** use Spark Streaming for area-level numbers.
4. **Sharding:** distribute historical pings by vehicle across database servers.
5. **Autoscaling:** scale workers based on Kafka backlog with KEDA.
6. **Reliability:** add alerts, tracing, and controlled failure tests.
7. **Batch analytics:** archive pings and analyze their full history with Spark.

Each phase will build on measured results from the previous one; we will not
start every infrastructure tool at once.
