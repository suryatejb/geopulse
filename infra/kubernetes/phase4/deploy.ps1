param(
    [double]$MinimumFreeGiB = 2.25,
    [switch]$SkipApiCutover
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
$image = 'citusdata/citus:14.2.0-pg17'
$namespace = 'geopulse'

if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw 'The GeoPulse kubeconfig is missing. Create the Phase 0 kind cluster first.'
}
$env:KUBECONFIG = $kubeconfig

$apiImage = 'geopulse-api:0.4.0'
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = "$userPath;$machinePath"
foreach ($tool in @('docker', 'kind', 'kubectl')) {
    if (-not (Get-Command $tool -ErrorAction SilentlyContinue)) {
        throw "$tool was not found on PATH."
    }
}

$freeMemoryGiB = (Get-CimInstance -ClassName Win32_OperatingSystem).FreePhysicalMemory / 1MB
if ($freeMemoryGiB -lt $MinimumFreeGiB) {
    throw ("Only {0:N2} GiB of host memory is free; need at least {1:N2} GiB " +
        'before starting the three Citus database pods.') -f $freeMemoryGiB, $MinimumFreeGiB
}

docker info --format '{{.OSType}}' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Docker is not responding. Start Docker Desktop before continuing.'
}
kubectl wait --for=condition=Ready nodes --all --timeout=90s
if ($LASTEXITCODE -ne 0) {
    throw 'The kind cluster is not fully ready.'
}
$readyNodes = & kubectl get nodes --no-headers
if ($LASTEXITCODE -ne 0 -or @($readyNodes).Count -ne 3) {
    throw 'Expected exactly three reachable kind nodes.'
}

$apiDeploymentJson = & kubectl -n $namespace get deployment geopulse-api -o json
if ($LASTEXITCODE -ne 0) {
    throw 'The GeoPulse API deployment was not found.'
}
$apiDeployment = $apiDeploymentJson | ConvertFrom-Json
$apiContainer = $apiDeployment.spec.template.spec.containers |
    Where-Object { $_.name -eq 'api' }
$apiIngestionMode = ($apiContainer.env |
    Where-Object { $_.name -eq 'PING_INGESTION_MODE' }).value
$originalDatabaseSecret = ($apiContainer.env |
    Where-Object { $_.name -eq 'DATABASE_URL' }).valueFrom.secretKeyRef.name
$apiReplicas = [int]$apiDeployment.spec.replicas
if ($apiIngestionMode -ne 'postgres') {
    throw 'Restore PostgreSQL ingestion mode before deploying Phase 4.'
}
if ($originalDatabaseSecret -notin @('geopulse-database', 'geopulse-citus-database')) {
    throw 'The API is not using a recognized PostgreSQL or Citus database secret.'
}
if ($SkipApiCutover -and $originalDatabaseSecret -eq 'geopulse-citus-database') {
    throw 'The API is already on Citus; -SkipApiCutover cannot switch it back to PostgreSQL.'
}
$writerDeployment = & kubectl -n $namespace get deployment geopulse-kafka-writer `
    --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) {
    throw 'Could not verify that the Kafka writer is stopped.'
}
if ($writerDeployment) {
    throw 'Stop the Kafka writer before migrating the PostgreSQL baseline to Citus.'
}

$sourceSecretJson = & kubectl -n $namespace get secret geopulse-database -o json
if ($LASTEXITCODE -ne 0) {
    throw 'The PostgreSQL baseline credentials were not found.'
}
$sourceSecret = $sourceSecretJson | ConvertFrom-Json
$databaseName = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($sourceSecret.data.POSTGRES_DB)
)
$databaseUser = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($sourceSecret.data.POSTGRES_USER)
)
$databasePassword = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String($sourceSecret.data.POSTGRES_PASSWORD)
)
$databaseUrl = 'postgresql://{0}:{1}@geopulse-citus-coordinator:5432/{2}' -f `
    [Uri]::EscapeDataString($databaseUser), `
    [Uri]::EscapeDataString($databasePassword), `
    [Uri]::EscapeDataString($databaseName)
$secretData = @{
    'POSTGRES_DB' = $sourceSecret.data.POSTGRES_DB
    'POSTGRES_USER' = $sourceSecret.data.POSTGRES_USER
    'POSTGRES_PASSWORD' = $sourceSecret.data.POSTGRES_PASSWORD
    'database-url' = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes($databaseUrl)
    )
}
$citusSecret = @{
    apiVersion = 'v1'
    kind = 'Secret'
    metadata = @{
        name = 'geopulse-citus-database'
        namespace = $namespace
    }
    type = 'Opaque'
    data = $secretData
} | ConvertTo-Json -Depth 5 -Compress
$citusSecret | & kubectl apply -f -
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to prepare Citus database credentials.'
}
Remove-Variable sourceSecretJson, sourceSecret, databaseName, databaseUser,
    databasePassword, databaseUrl, secretData, citusSecret

if (-not $SkipApiCutover) {
    Push-Location $repoRoot
    try {
        docker build --tag $apiImage .
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to build the Phase 4 API image.'
        }
    } finally {
        Pop-Location
    }
    kind load docker-image $apiImage --name geopulse
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to load the Phase 4 API image into kind.'
    }
}

docker pull $image
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to pull the pinned Citus image.'
}
kind load docker-image $image --name geopulse
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to load the Citus image into kind.'
}

kubectl apply -f (Join-Path $PSScriptRoot 'citus.yaml')
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create the Citus coordinator and workers.'
}
kubectl -n $namespace rollout status statefulset/geopulse-citus-coordinator --timeout=300s
if ($LASTEXITCODE -ne 0) {
    throw 'The Citus coordinator did not become ready.'
}
kubectl -n $namespace rollout status statefulset/geopulse-citus-workers --timeout=300s
if ($LASTEXITCODE -ne 0) {
    throw 'The two Citus workers did not become ready.'
}

$bootstrapSql = Join-Path $PSScriptRoot 'bootstrap.sql'
$configMap = & kubectl -n $namespace create configmap geopulse-citus-bootstrap `
    "--from-file=bootstrap.sql=$bootstrapSql" --dry-run=client -o yaml
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to prepare the Citus bootstrap SQL.'
}
$configMap | & kubectl apply -f -
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to apply the Citus bootstrap configuration.'
}
kubectl -n $namespace delete job geopulse-citus-bootstrap --ignore-not-found --wait=true
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to remove the previous Citus bootstrap job.'
}
kubectl apply -f (Join-Path $PSScriptRoot 'bootstrap-job.yaml')
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to start the Citus bootstrap job.'
}
kubectl -n $namespace wait --for=condition=complete job/geopulse-citus-bootstrap --timeout=300s
if ($LASTEXITCODE -ne 0) {
    kubectl -n $namespace logs job/geopulse-citus-bootstrap --all-containers=true
    throw 'The Citus cluster or sharded pings table failed to initialize.'
}

$apiPaused = $false
$cutoverAttempted = $false
try {
    kubectl -n $namespace scale deployment/geopulse-api --replicas=0
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to pause API writes for the baseline data copy.'
    }
    $apiPaused = $true
    kubectl -n $namespace wait --for=delete pod -l app=geopulse-api --timeout=120s
    if ($LASTEXITCODE -ne 0) {
        throw 'The API did not stop before data migration.'
    }

    kubectl -n $namespace delete job geopulse-citus-migrate --ignore-not-found --wait=true
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to remove the previous Citus migration job.'
    }
    kubectl apply -f (Join-Path $PSScriptRoot 'migrate.yaml')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to start the baseline data migration.'
    }
    kubectl -n $namespace wait --for=condition=complete job/geopulse-citus-migrate --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        kubectl -n $namespace logs job/geopulse-citus-migrate --all-containers=true
        throw 'The PostgreSQL baseline data was not migrated to Citus.'
    }

    if ($SkipApiCutover) {
        kubectl apply -f (Join-Path $repoRoot 'infra\kubernetes\phase1\api.yaml')
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to restore the baseline PostgreSQL API configuration.'
        }
    } else {
        $cutoverAttempted = $true
        kubectl apply -f (Join-Path $PSScriptRoot 'api-citus.yaml')
        if ($LASTEXITCODE -ne 0) {
            throw 'Failed to point the API at the Citus coordinator.'
        }
    }
    kubectl -n $namespace scale deployment/geopulse-api "--replicas=$apiReplicas"
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to restart the API after database migration.'
    }
    kubectl -n $namespace rollout status deployment/geopulse-api --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw 'The API did not become ready after database migration.'
    }
    if (-not $SkipApiCutover) {
        kubectl -n $namespace exec deployment/geopulse-api -- python -c `
            "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode())"
        if ($LASTEXITCODE -ne 0) {
            throw 'The API health check failed after the Citus cutover.'
        }
    }
    $apiPaused = $false
} catch {
    $deploymentError = $_
    if ($cutoverAttempted -and $originalDatabaseSecret -ne 'geopulse-citus-database') {
        throw ("Phase 4 failed after Citus cutover began. The API configuration was not " +
            "automatically switched back because it may have accepted Citus writes. " +
            "Inspect the API and use rollback.ps1 after checking the data. Original error: $deploymentError")
    }
    if ($apiPaused) {
        if ($originalDatabaseSecret -eq 'geopulse-citus-database') {
            kubectl apply -f (Join-Path $PSScriptRoot 'api-citus.yaml')
        } else {
            kubectl apply -f (Join-Path $repoRoot 'infra\kubernetes\phase1\api.yaml')
        }
        if ($LASTEXITCODE -ne 0) {
            throw 'Phase 4 failed and the API database configuration could not be restored.'
        }
        kubectl -n $namespace scale deployment/geopulse-api "--replicas=$apiReplicas"
        if ($LASTEXITCODE -ne 0) {
            throw 'Phase 4 failed and the API could not be restarted.'
        }
        kubectl -n $namespace rollout status deployment/geopulse-api --timeout=180s
        if ($LASTEXITCODE -ne 0) {
            throw 'Phase 4 failed and the API did not recover.'
        }
    }
    throw $deploymentError
}

kubectl -n $namespace exec statefulset/geopulse-citus-coordinator -- psql `
    -U geopulse -d geopulse -c `
    "SELECT nodename, nodeport FROM pg_dist_node WHERE isactive AND noderole = 'primary' ORDER BY nodename;"
if ($LASTEXITCODE -ne 0) {
    throw 'Could not verify the registered Citus workers.'
}
kubectl -n $namespace exec statefulset/geopulse-citus-coordinator -- psql `
    -U geopulse -d geopulse -c `
    "SELECT COUNT(*) AS shard_count FROM pg_dist_shard WHERE logicalrelid = 'public.pings'::regclass;"
if ($LASTEXITCODE -ne 0) {
    throw 'Could not verify the pings shard count.'
}

Write-Output 'Phase 4 Citus coordinator and two workers are ready.'
if ($SkipApiCutover) {
    Write-Output 'The API remains on the PostgreSQL baseline; pass no switch to deploy.ps1 to cut over.'
} else {
    Write-Output 'The API is using the migrated Citus database. Run rollback.ps1 to return to the baseline.'
}
