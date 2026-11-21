import http from 'k6/http';
import { check } from 'k6';
import { Counter } from 'k6/metrics';

const rate = Number(__ENV.RATE || 200);
const duration = __ENV.DURATION || '30s';
const url = __ENV.API_URL || 'http://geopulse-api.geopulse.svc.cluster.local:8000/api/v1/pings';
const timeout = __ENV.TIMEOUT || '10s';
const acceptedPings = new Counter('accepted_pings');

export const options = {
  scenarios: {
    load: {
      executor: 'constant-arrival-rate',
      rate,
      timeUnit: '1s',
      duration,
      preAllocatedVUs: Math.min(Math.max(Math.floor(rate / 4), 50), 1500),
      maxVUs: 3000,
    },
  },
  summaryTrendStats: ['med', 'p(95)', 'p(99)', 'max'],
};

export default function () {
  const vehicle = __VU;
  const runId = __ENV.TEST_RUN_ID || 'local';
  const body = JSON.stringify({
    vehicle_id: `vehicle-${runId}-${String(vehicle).padStart(5, '0')}`,
    sequence_number: __ITER + 1,
    latitude: 40.7 + Math.random() * 0.1,
    longitude: -74.02 + Math.random() * 0.12,
    speed_kmh: Math.random() * 80,
    recorded_at: new Date().toISOString(),
  });
  const response = http.post(url, body, {
    headers: { 'Content-Type': 'application/json' },
    timeout,
  });
  const accepted = response.status === 201 || response.status === 202;
  check(response, { accepted: () => accepted });
  if (accepted) acceptedPings.add(1);
}

function metric(data, name, key) {
  const m = data.metrics[name];
  return m && m.values[key] !== undefined ? m.values[key] : 0;
}

export function handleSummary(data) {
  const result = {
    target_rate: rate,
    requests: metric(data, 'http_reqs', 'count'),
    accepted: metric(data, 'accepted_pings', 'count'),
    achieved_per_s: Number(metric(data, 'http_reqs', 'rate').toFixed(1)),
    failed_ratio: Number(metric(data, 'http_req_failed', 'rate').toFixed(4)),
    dropped: metric(data, 'dropped_iterations', 'count'),
    p50_ms: Number(metric(data, 'http_req_duration', 'med').toFixed(1)),
    p95_ms: Number(metric(data, 'http_req_duration', 'p(95)').toFixed(1)),
    p99_ms: Number(metric(data, 'http_req_duration', 'p(99)').toFixed(1)),
    max_ms: Number(metric(data, 'http_req_duration', 'max').toFixed(1)),
    max_vus: metric(data, 'vus_max', 'max'),
  };
  return { stdout: `RESULT ${JSON.stringify(result)}\n` };
}
