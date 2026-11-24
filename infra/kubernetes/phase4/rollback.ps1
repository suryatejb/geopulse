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

$migrationRows = & kubectl -n geopulse exec geopulse-citus-coordinator-0 -- psql `
    -U geopulse -d geopulse -Atc `
    "SELECT source_rows FROM public.phase4_migration_state WHERE migration_key = 'baseline-pings'"
if ($LASTEXITCODE -ne 0 -or -not $migrationRows) {
    throw 'Could not verify the baseline-to-Citus migration marker.'
}
$citusRows = & kubectl -n geopulse exec geopulse-citus-coordinator-0 -- psql `
    -U geopulse -d geopulse -Atc "SELECT COUNT(*) FROM public.pings"
if ($LASTEXITCODE -ne 0) {
    throw 'Could not count pings in Citus before rollback.'
}
if ([long]$citusRows -gt [long]$migrationRows -and -not $AllowDataLoss) {
    throw ("Citus contains {0} rows beyond the {1} migrated baseline rows. " +
        'Use -AllowDataLoss only if you intentionally accept losing those newer rows.')
}

kubectl apply -f (Join-Path $repoRoot 'infra\kubernetes\phase1\api.yaml')
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to restore the baseline PostgreSQL API configuration.'
}
kubectl -n geopulse delete deployment geopulse-kafka-writer --ignore-not-found
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to stop the Kafka writer while restoring baseline mode.'
}
kubectl -n geopulse rollout status deployment/geopulse-api --timeout=180s
if ($LASTEXITCODE -ne 0) {
    throw 'The API did not return to the PostgreSQL baseline.'
}

Write-Output 'The API now uses the original PostgreSQL database. The Citus cluster and its data were preserved.'
