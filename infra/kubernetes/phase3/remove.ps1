param(
    [switch]$DeleteData,
    [switch]$RemoveOperator
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw 'The GeoPulse kubeconfig is missing.'
}
$env:KUBECONFIG = $kubeconfig

kubectl delete -f (Join-Path $PSScriptRoot 'spark-application.yaml') --ignore-not-found=true --wait=true
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to remove the Phase 3 SparkApplication.'
}
kubectl delete -f (Join-Path $PSScriptRoot 'redis.yaml') --ignore-not-found=true --wait=true
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to remove the Phase 3 Redis StatefulSet and service.'
}

if ($DeleteData) {
    kubectl -n geopulse delete pvc geopulse-phase3-checkpoint data-geopulse-phase3-redis-0 `
        --ignore-not-found=true --wait=true
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to delete the Phase 3 checkpoint or Redis data.'
    }
} else {
    Write-Output 'Checkpoint and Redis PVCs were preserved. Pass -DeleteData to remove them.'
}

if ($RemoveOperator) {
    helm uninstall geopulse-spark-operator --namespace spark-operator --wait
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to remove the Spark Operator Helm release.'
    }
}
