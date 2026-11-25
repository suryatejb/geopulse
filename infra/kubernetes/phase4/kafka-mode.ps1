param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('kafka', 'postgres')]
    [string]$Mode,
    [double]$MinimumFreeGiB = 2.25
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw 'The GeoPulse kubeconfig is missing.'
}
$env:KUBECONFIG = $kubeconfig
$namespace = 'geopulse'
$apiImage = 'geopulse-api:0.4.1'

$deploymentJson = & kubectl -n $namespace get deployment geopulse-api -o json
if ($LASTEXITCODE -ne 0) {
    throw 'Could not inspect the API deployment.'
}
$deployment = $deploymentJson | ConvertFrom-Json
$api = $deployment.spec.template.spec.containers | Where-Object { $_.name -eq 'api' }
$databaseSecret = ($api.env | Where-Object { $_.name -eq 'DATABASE_URL' }).valueFrom.secretKeyRef.name
$ingestionMode = ($api.env | Where-Object { $_.name -eq 'PING_INGESTION_MODE' }).value
$apiTopic = $api.env | Where-Object { $_.name -eq 'KAFKA_TOPIC' }
$apiServers = ($api.env | Where-Object { $_.name -eq 'KAFKA_BOOTSTRAP_SERVERS' }).value
$replicas = [int]$deployment.spec.replicas
if ($databaseSecret -ne 'geopulse-citus-database' -or
    $ingestionMode -notin @('postgres', 'kafka') -or
    ($apiTopic -and $apiTopic.value -ne 'pings') -or
    $apiServers -ne 'geopulse-kafka-bootstrap.kafka.svc:9092' -or $replicas -lt 1) {
    throw 'Expected a running Citus API using the default pings topic.'
}

$writerJson = & kubectl -n $namespace get deployment geopulse-kafka-writer --ignore-not-found -o json
if ($LASTEXITCODE -ne 0) {
    throw 'Could not inspect the Kafka writer deployment.'
}

if ($Mode -eq 'kafka') {
    if ($ingestionMode -ne 'postgres' -or $writerJson) {
        throw 'Kafka mode requires the Citus API in direct mode and no existing writer deployment.'
    }
    $freeMemoryGiB = (Get-CimInstance -ClassName Win32_OperatingSystem).FreePhysicalMemory / 1MB
    if ($freeMemoryGiB -lt $MinimumFreeGiB) {
        throw ("Only {0:N2} GiB of host memory is free; need at least {1:N2} GiB " +
            'before starting the Kafka writer.') -f $freeMemoryGiB, $MinimumFreeGiB
    }
    kubectl -n kafka wait kafka/geopulse --for=condition=Ready --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        throw 'Kafka is not ready.'
    }
    Push-Location $repoRoot
    try {
        docker build --tag $apiImage .
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to build the Kafka/Citus image.'
        }
    } finally {
        Pop-Location
    }
    kind load docker-image $apiImage --name geopulse
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to load the Kafka/Citus image into kind.'
    }
    kubectl -n $namespace set image deployment/geopulse-api "api=$apiImage"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to update the Citus API image.'
    }
    kubectl -n $namespace rollout status deployment/geopulse-api --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw 'The updated Citus API did not become ready.'
    }

    $manifestJson = & kubectl create -f (Join-Path $repoRoot 'infra\kubernetes\phase2\writer.yaml') `
        --dry-run=client -o json
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not prepare the Kafka writer deployment.'
    }
    $manifest = $manifestJson | ConvertFrom-Json
    $writerContainer = $manifest.spec.template.spec.containers | Where-Object { $_.name -eq 'kafka-writer' }
    $writerContainer.image = $apiImage
    ($writerContainer.env | Where-Object { $_.name -eq 'DATABASE_URL' }).valueFrom.secretKeyRef.name =
        'geopulse-citus-database'
    $manifest | ConvertTo-Json -Depth 100 -Compress | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to deploy the Citus Kafka writer. Inspect the writer before retrying.'
    }
    kubectl -n $namespace rollout status deployment/geopulse-kafka-writer --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw 'The Citus Kafka writer did not become ready; inspect it before retrying.'
    }
    kubectl -n $namespace set env deployment/geopulse-api PING_INGESTION_MODE=kafka
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not enable Kafka ingestion. Inspect the API and writer before retrying.'
    }
    kubectl -n $namespace rollout status deployment/geopulse-api --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw 'Kafka ingestion rollout failed. Inspect the API and writer before retrying.'
    }
    Write-Output 'The API publishes to Kafka and the writer targets Citus.'
    return
}

if (-not $writerJson) {
    throw 'The Citus Kafka writer deployment is missing; refusing to assume Kafka is drained.'
}
$writer = $writerJson | ConvertFrom-Json
$container = $writer.spec.template.spec.containers | Where-Object { $_.name -eq 'kafka-writer' }
$writerSecret = ($container.env | Where-Object { $_.name -eq 'DATABASE_URL' }).valueFrom.secretKeyRef.name
$writerTopic = $container.env | Where-Object { $_.name -eq 'KAFKA_TOPIC' }
$writerGroup = $container.env | Where-Object { $_.name -eq 'KAFKA_GROUP_ID' }
$writerServers = ($container.env | Where-Object { $_.name -eq 'KAFKA_BOOTSTRAP_SERVERS' }).value
if ($writerSecret -ne 'geopulse-citus-database' -or [int]$writer.spec.replicas -ne 1 -or
    ($writerTopic -and $writerTopic.value -ne 'pings') -or
    ($writerGroup -and $writerGroup.value -ne 'geopulse-ping-writer') -or
    $writerServers -ne 'geopulse-kafka-bootstrap.kafka.svc:9092') {
    throw 'Expected one Citus writer using the default topic and consumer group.'
}

$apiPaused = $false
$writerStopAttempted = $false
try {
    kubectl -n $namespace scale deployment/geopulse-api --replicas=0
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to pause the API.'
    }
    $apiPaused = $true
    kubectl -n $namespace wait --for=delete pod -l app=geopulse-api --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        throw 'The API did not stop before draining Kafka.'
    }
    kubectl -n $namespace exec deployment/geopulse-kafka-writer -- python -m app.kafka_drain
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not confirm the Kafka writer drained all retained messages.'
    }
    $writerStopAttempted = $true
    kubectl -n $namespace delete deployment geopulse-kafka-writer --wait=true
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to stop the Kafka writer.'
    }
    kubectl -n $namespace wait --for=delete pod -l app=geopulse-kafka-writer --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        throw 'The Kafka writer did not stop.'
    }
    kubectl -n $namespace set env deployment/geopulse-api PING_INGESTION_MODE=postgres
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to restore direct Citus ingestion.'
    }
    kubectl -n $namespace scale deployment/geopulse-api "--replicas=$replicas"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to restart the Citus API.'
    }
    kubectl -n $namespace rollout status deployment/geopulse-api --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw 'The direct-write Citus API did not become ready.'
    }
    $apiPaused = $false
} catch {
    $modeError = $_
    if ($writerStopAttempted) {
        throw ("The switch failed after writer shutdown began; inspect the API and writer " +
            "before accepting more traffic. Original error: $modeError")
    }
    if ($apiPaused) {
        kubectl -n $namespace scale deployment/geopulse-api "--replicas=$replicas"
        if ($LASTEXITCODE -ne 0) {
            throw "Could not restart the original API mode. Original error: $modeError"
        }
        kubectl -n $namespace rollout status deployment/geopulse-api --timeout=180s
        if ($LASTEXITCODE -ne 0) {
            throw "The original API mode did not recover. Original error: $modeError"
        }
    }
    throw $modeError
}
Write-Output 'Kafka is drained and the API is using direct Citus writes.'
