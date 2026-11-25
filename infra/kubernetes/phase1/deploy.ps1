param(
    [double]$MinimumFreeGiB = 2,
    [ValidateSet('postgres', 'kafka')]
    [string]$IngestionMode = 'postgres'
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
$image = 'geopulse-api:0.3.0'
$namespace = 'geopulse'

$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = "$userPath;$machinePath"

if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw 'The GeoPulse kubeconfig is missing. Create the Phase 0 kind cluster first.'
}

$dockerCommand = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCommand) {
    $dockerPath = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\docker.exe'
    if (Test-Path -LiteralPath $dockerPath) {
        $dockerCommand = Get-Item -LiteralPath $dockerPath
    } else {
        throw 'Docker was not found. Start Docker Desktop and refresh PATH.'
    }
}
$docker = $dockerCommand.Source
if (-not $docker) {
    $docker = $dockerCommand.FullName
}

$kindCommand = Get-Command kind -ErrorAction SilentlyContinue
if (-not $kindCommand) {
    throw 'kind was not found. Install kind and refresh PATH.'
}
$kind = $kindCommand.Source
if (-not $kind) {
    $kind = $kindCommand.FullName
}

$kubectlCommand = Get-Command kubectl -ErrorAction SilentlyContinue
if (-not $kubectlCommand) {
    $kubectlPath = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\kubectl.exe'
    if (Test-Path -LiteralPath $kubectlPath) {
        $kubectlCommand = Get-Item -LiteralPath $kubectlPath
    } else {
        throw 'kubectl was not found. Install kubectl and refresh PATH.'
    }
}
$kubectl = $kubectlCommand.Source
if (-not $kubectl) {
    $kubectl = $kubectlCommand.FullName
}
$env:KUBECONFIG = $kubeconfig

& $docker info --format '{{.OSType}}'
if ($LASTEXITCODE -ne 0) {
    throw 'Docker is not responding. Start Docker Desktop before continuing.'
}

& $kubectl wait --for=condition=Ready nodes --all --timeout=90s
if ($LASTEXITCODE -ne 0) {
    throw 'The kind cluster is not fully ready.'
}

if ($IngestionMode -eq 'kafka') {
    & $kubectl -n kafka wait kafka/geopulse --for=condition=Ready --timeout=600s
    if ($LASTEXITCODE -ne 0) {
        throw 'The Kafka cluster is not ready. Install the Phase 2 Kafka infrastructure first.'
    }
}

$os = Get-CimInstance -ClassName Win32_OperatingSystem
$availableMemoryGiB = $os.FreePhysicalMemory / 1MB
if ($availableMemoryGiB -lt $MinimumFreeGiB) {
    $memoryMessage = ("Only {0:N2} GiB of host memory is available; close other applications " +
        "before adding the API and database to the cluster.") -f $availableMemoryGiB
    throw $memoryMessage
}

$readyNodes = & $kubectl get nodes --no-headers
if ($LASTEXITCODE -ne 0 -or @($readyNodes).Count -ne 3) {
    throw 'Expected exactly three reachable kind nodes.'
}

$existingApiJson = & $kubectl -n $namespace get deployment geopulse-api --ignore-not-found -o json
if ($LASTEXITCODE -ne 0) {
    throw 'Could not inspect the existing API deployment before restoring baseline mode.'
}
if ($existingApiJson) {
    $existingApi = $existingApiJson | ConvertFrom-Json
    $existingApiContainer = $existingApi.spec.template.spec.containers |
        Where-Object { $_.name -eq 'api' }
    $existingDatabaseSecret = ($existingApiContainer.env |
        Where-Object { $_.name -eq 'DATABASE_URL' }).valueFrom.secretKeyRef.name
    if ($existingDatabaseSecret -eq 'geopulse-citus-database') {
        throw 'The API is using Citus. Run phase4\rollback.ps1 before deploying the PostgreSQL baseline or Kafka mode.'
    }
}

& $kubectl create namespace $namespace --dry-run=client -o yaml | & $kubectl apply -f -
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create namespace '$namespace'."
}

$secretName = & $kubectl -n $namespace get secret geopulse-database --ignore-not-found -o name
if ($LASTEXITCODE -ne 0) {
    throw 'Could not check the database secret.'
}

if (-not $secretName) {
    $randomBytes = New-Object byte[] 32
    $random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $random.GetBytes($randomBytes)
    } finally {
        $random.Dispose()
    }
    $password = [Convert]::ToBase64String($randomBytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
    $databaseUrl = "postgresql://geopulse:$password@postgres:5432/geopulse"
    $secretJson = @{
        apiVersion = 'v1'
        kind = 'Secret'
        metadata = @{
            name = 'geopulse-database'
            namespace = $namespace
        }
        type = 'Opaque'
        data = @{
            'POSTGRES_DB' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('geopulse'))
            'POSTGRES_USER' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('geopulse'))
            'POSTGRES_PASSWORD' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($password))
            'database-url' = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($databaseUrl))
        }
    } | ConvertTo-Json -Depth 5 -Compress

    $secretJson | & $kubectl apply -f -
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to create the database credentials secret.'
    }
    Remove-Variable randomBytes, password, databaseUrl, secretJson
}

Push-Location $repoRoot
try {
    & $docker build --tag $image .
    if ($LASTEXITCODE -ne 0) {
        throw 'Docker image build failed.'
    }
} finally {
    Pop-Location
}

& $kind load docker-image $image --name geopulse
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to load the API image into kind.'
}

foreach ($manifest in @('database.yaml', 'api.yaml')) {
    & $kubectl apply -f (Join-Path $PSScriptRoot $manifest)
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to apply manifest '$manifest'."
    }
}

& $kubectl -n $namespace rollout restart deployment/geopulse-api
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to restart the API with the newly built image.'
}

if ($IngestionMode -eq 'kafka') {
    & $kubectl -n $namespace set env deployment/geopulse-api PING_INGESTION_MODE=kafka
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to enable Kafka ingestion.'
    }
    & $kubectl apply -f (Join-Path $repoRoot 'infra\kubernetes\phase2\writer.yaml')
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to deploy the Kafka ping writer.'
    }
    & $kubectl -n $namespace rollout restart deployment/geopulse-kafka-writer
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to restart the Kafka ping writer with the newly built image.'
    }
    & $kubectl -n $namespace rollout status deployment/geopulse-kafka-writer --timeout=180s
    if ($LASTEXITCODE -ne 0) {
        throw 'The Kafka ping writer did not become ready in time.'
    }
} else {
    & $kubectl -n $namespace delete deployment geopulse-kafka-writer --ignore-not-found
}

& $kubectl -n $namespace rollout status statefulset/postgres --timeout=180s
if ($LASTEXITCODE -ne 0) {
    throw 'PostgreSQL did not become ready in time.'
}

& $kubectl -n $namespace rollout status deployment/geopulse-api --timeout=180s
if ($LASTEXITCODE -ne 0) {
    throw 'The API did not become ready in time.'
}

& $kubectl -n $namespace get pods,services,persistentvolumeclaims
if ($LASTEXITCODE -ne 0) {
    throw 'Could not verify the Kubernetes resources.'
}
