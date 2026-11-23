param(
    [double]$MinimumFreeGiB = 2.25,
    [int]$StartupTimeoutSeconds = 600,
    [switch]$SkipSparkApplication
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
$image = 'geopulse-phase3-spark:0.2.1'
$namespace = 'geopulse'

if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw 'The GeoPulse kubeconfig is missing. Create the Phase 0 kind cluster first.'
}
$env:KUBECONFIG = $kubeconfig

foreach ($tool in @('docker', 'kind', 'kubectl', 'helm')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found on PATH."
    }
}

$freeMemoryGiB = (Get-CimInstance -ClassName Win32_OperatingSystem).FreePhysicalMemory / 1MB
if ($freeMemoryGiB -lt $MinimumFreeGiB) {
    throw ("Only {0:N2} GiB of host memory is free; need at least {1:N2} GiB " +
        'before starting the Spark driver and executor.') -f $freeMemoryGiB, $MinimumFreeGiB
}

docker info --format '{{.OSType}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker is not responding. Start Docker Desktop before continuing.'
}
kubectl cluster-info | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'The GeoPulse Kubernetes cluster is not reachable.'
}

Push-Location $repoRoot
try {
    docker build --file Dockerfile.streaming --tag $image .
    if ($LASTEXITCODE -ne 0) {
        throw 'The Phase 3 Spark image build failed.'
    }
} finally {
    Pop-Location
}

kind load docker-image $image --name geopulse
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to load the Phase 3 image into the kind cluster.'
}

helm repo add spark-operator https://kubeflow.github.io/spark-operator --force-update
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to configure the Spark Operator Helm repository.'
}
helm repo update spark-operator
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to update the Spark Operator Helm repository.'
}

$operatorValues = Join-Path $PSScriptRoot 'spark-operator-values.yaml'
helm upgrade --install geopulse-spark-operator spark-operator/spark-operator `
    --namespace spark-operator `
    --create-namespace `
    --version 2.6.0 `
    --values $operatorValues `
    --wait `
    --timeout 5m
if ($LASTEXITCODE -ne 0) {
    throw 'The Spark Operator did not become ready.'
}

kubectl -n kafka wait kafkatopic/pings --for=condition=Ready --timeout=120s
if ($LASTEXITCODE -ne 0) {
    throw 'The existing Kafka pings topic is not ready.'
}

$manifests = @('redis.yaml', 'checkpoint-pvc.yaml')
if (-not $SkipSparkApplication) {
    $manifests += 'spark-application.yaml'
}

foreach ($manifest in $manifests) {
    kubectl apply -f (Join-Path $PSScriptRoot $manifest)
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply Phase 3 manifest '$manifest'."
    }
}

kubectl -n $namespace rollout status statefulset/geopulse-phase3-redis --timeout=180s
if ($LASTEXITCODE -ne 0) {
    throw 'The persistent Phase 3 Redis instance did not become ready.'
}

if ($SkipSparkApplication) {
    Write-Output 'Phase 3 infrastructure is ready; the live SparkApplication was not started.'
    kubectl -n $namespace get pods,pvc
    exit 0
}

$deadline = [DateTime]::UtcNow.AddSeconds($StartupTimeoutSeconds)
while ([DateTime]::UtcNow -lt $deadline) {
    $state = & kubectl -n $namespace get sparkapplication geopulse-live-statistics `
        -o 'jsonpath={.status.applicationState.state}' 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not read the SparkApplication status.'
    }
    if ($state -eq 'RUNNING') {
        Write-Output 'Phase 3 Spark streaming is running.'
        kubectl -n $namespace get sparkapplication,pods,pvc
        exit 0
    }
    if ($state -eq 'FAILED' -or $state -eq 'FAILING' -or $state -eq 'SUBMISSION_FAILED') {
        kubectl -n $namespace describe sparkapplication geopulse-live-statistics
        throw "SparkApplication entered state '$state'."
    }
    Start-Sleep -Seconds 5
}

kubectl -n $namespace describe sparkapplication geopulse-live-statistics
throw "SparkApplication did not reach RUNNING within $StartupTimeoutSeconds seconds."
