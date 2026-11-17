param(
    [double]$MinimumFreeGiB = 2
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
$image = 'geopulse-api:0.1.1'
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
        throw "Failed to apply Phase 1 manifest '$manifest'."
    }
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
    throw 'Could not verify the Phase 1 Kubernetes resources.'
}
