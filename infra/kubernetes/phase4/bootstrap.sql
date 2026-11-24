SELECT citus_add_node(
    'geopulse-citus-workers-0.geopulse-citus-workers.geopulse.svc.cluster.local',
    5432
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_dist_node
    WHERE nodename = 'geopulse-citus-workers-0.geopulse-citus-workers.geopulse.svc.cluster.local'
      AND nodeport = 5432
      AND isactive
);

SELECT citus_add_node(
    'geopulse-citus-workers-1.geopulse-citus-workers.geopulse.svc.cluster.local',
    5432
)
WHERE NOT EXISTS (
    SELECT 1 FROM pg_dist_node
    WHERE nodename = 'geopulse-citus-workers-1.geopulse-citus-workers.geopulse.svc.cluster.local'
      AND nodeport = 5432
      AND isactive
);

CREATE TABLE IF NOT EXISTS public.pings (
    id BIGSERIAL NOT NULL,
    vehicle_id TEXT NOT NULL,
    sequence_number BIGINT,
    latitude DOUBLE PRECISION NOT NULL,
    longitude DOUBLE PRECISION NOT NULL,
    speed_kmh DOUBLE PRECISION NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT pings_pkey PRIMARY KEY (vehicle_id, id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_pings_vehicle_sequence
ON public.pings (vehicle_id, sequence_number);

CREATE INDEX IF NOT EXISTS idx_pings_vehicle_recorded
ON public.pings (vehicle_id, recorded_at DESC);

SELECT create_distributed_table('public.pings', 'vehicle_id')
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_dist_partition
    WHERE logicalrelid = 'public.pings'::regclass
);

CREATE TABLE IF NOT EXISTS public.phase4_migration_state (
    migration_key TEXT PRIMARY KEY,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    source_rows BIGINT NOT NULL
);

SELECT create_reference_table('public.phase4_migration_state')
WHERE NOT EXISTS (
    SELECT 1
    FROM pg_dist_partition
    WHERE logicalrelid = 'public.phase4_migration_state'::regclass
);
