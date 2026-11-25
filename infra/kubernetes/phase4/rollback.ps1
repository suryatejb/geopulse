param(
    [switch]$AllowDataLoss
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw 'The GeoPulse kubeconfig is missing.'
}
$env:KUBECONFIG = $kubeconfig

$deploymentJson = & kubectl -n geopulse get deployment geopulse-api -o json
if ($LASTEXITCODE -ne 0) {
    throw 'Could not inspect the API deployment.'
}
$deployment = $deploymentJson | ConvertFrom-Json
$api = $deployment.spec.template.spec.containers | Where-Object { $_.name -eq 'api' }
$databaseSecret = ($api.env | Where-Object { $_.name -eq 'DATABASE_URL' }).valueFrom.secretKeyRef.name
$ingestionMode = ($api.env | Where-Object { $_.name -eq 'PING_INGESTION_MODE' }).value
$replicas = [int]$deployment.spec.replicas
if ($databaseSecret -ne 'geopulse-citus-database') {
    throw 'The API is not configured to use Citus; refusing rollback.'
}
if ($ingestionMode -ne 'postgres') {
    throw 'Return the Citus API to direct PostgreSQL ingestion before rolling back.'
}
$writer = & kubectl -n geopulse get deployment geopulse-kafka-writer --ignore-not-found -o name
if ($LASTEXITCODE -ne 0 -or $writer) {
    throw 'Could not verify that the Kafka writer is stopped; refusing rollback.'
}

$paused = $false
$baselineApplied = $false
try {
    kubectl -n geopulse scale deployment/geopulse-api --replicas=0
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to pause API writes before rollback.'
    }
    $paused = $true
    kubectl -n geopulse wait --for=delete pod -l app=geopulse-api --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        throw 'The API did not stop before rollback.'
    }

    $compareScript = @'
set -euo pipefail
export PGUSER="$POSTGRES_USER"
sql='COPY (SELECT id, vehicle_id, sequence_number, latitude, longitude, speed_kmh, recorded_at, received_at FROM public.pings ORDER BY id, vehicle_id) TO STDOUT WITH (FORMAT binary)'
psql -h postgres.geopulse.svc.cluster.local -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$sql" | sha256sum
psql -h geopulse-citus-coordinator -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 -c "$sql" | sha256sum
printf '\n'
'@
    $compareScript = $compareScript.Replace("`r", '')
    $output = $compareScript | & kubectl -n geopulse exec -i geopulse-citus-coordinator-0 -- bash
    $hashes = @($output | Where-Object { $_.Trim() -ne '' })
    if ($LASTEXITCODE -ne 0 -or $hashes.Count -ne 2 -or
        $hashes[0] -notmatch '^[0-9a-f]{64}  -$' -or
        $hashes[1] -notmatch '^[0-9a-f]{64}  -$') {
        throw 'Could not compare PostgreSQL and Citus data before rollback.'
    }
    if ($hashes[0] -ne $hashes[1] -and -not $AllowDataLoss) {
        throw ('PostgreSQL and Citus contain different pings. Use -AllowDataLoss only ' +
            'if you intentionally accept leaving Citus changes behind.')
    }

    $baselineApplied = $true
    kubectl apply -f (Join-Path $repoRoot 'infra\kubernetes\phase1\api.yaml')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to restore the baseline PostgreSQL API configuration.'
    }
    kubectl -n geopulse scale deployment/geopulse-api "--replicas=$replicas"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to restore the API replica count.'
    }
    kubectl -n geopulse rollout status deployment/geopulse-api --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw 'The API did not return to the PostgreSQL baseline.'
    }
    $paused = $false
} catch {
    $rollbackError = $_
    if ($baselineApplied) {
        throw ("Rollback failed after the baseline switch began. The API configuration " +
            "was not automatically switched back because it may have accepted writes. " +
            "Inspect both databases and the deployment. Original error: $rollbackError")
    }
    if ($paused) {
        kubectl -n geopulse scale deployment/geopulse-api "--replicas=$replicas"
        if ($LASTEXITCODE -ne 0) {
            throw "Rollback failed and the Citus API could not be restarted. Original error: $rollbackError"
        }
        kubectl -n geopulse rollout status deployment/geopulse-api --timeout=180s
        if ($LASTEXITCODE -ne 0) {
            throw "Rollback failed and the Citus API did not recover. Original error: $rollbackError"
        }
    }
    throw $rollbackError
}

Write-Output 'The API now uses the original PostgreSQL database. The Citus cluster and its data were preserved.'
