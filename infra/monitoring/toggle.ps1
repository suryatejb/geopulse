param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('on', 'off')]
    [string]$State
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$machinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$env:Path = "$userPath;$machinePath"
$env:KUBECONFIG = Join-Path $repoRoot '.geopulse\kubeconfig'

$kubectlCommand = Get-Command kubectl -ErrorAction SilentlyContinue
if ($kubectlCommand) {
    $kubectl = $kubectlCommand.Source
} else {
    $kubectl = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\resources\bin\kubectl.exe'
}

$replicas = if ($State -eq 'on') { 1 } else { 0 }
$namespace = 'monitoring'
$prometheus = 'geopulse-monitoring-kube-p-prometheus'

# Prometheus is managed by the operator, so change its replica count in its own resource.
& $kubectl -n $namespace scale prometheus $prometheus --replicas=$replicas
if ($LASTEXITCODE -ne 0) { throw 'Could not change Prometheus.' }

# The operator may already be stopped, so also scale Prometheus's workload directly.
& $kubectl -n $namespace scale statefulset "prometheus-$prometheus" --replicas=$replicas
if ($LASTEXITCODE -ne 0) { throw 'Could not scale the Prometheus workload.' }

foreach ($deployment in @(
    'geopulse-monitoring-grafana',
    'geopulse-monitoring-kube-state-metrics',
    'geopulse-monitoring-kube-p-operator'
)) {
    & $kubectl -n $namespace scale deployment $deployment --replicas=$replicas
    if ($LASTEXITCODE -ne 0) { throw "Could not scale $deployment." }
}

if ($State -eq 'on') {
    & $kubectl -n $namespace rollout status deployment/geopulse-monitoring-grafana --timeout=180s
    if ($LASTEXITCODE -ne 0) { throw 'Grafana did not become ready.' }
}

& $kubectl -n $namespace get pods
