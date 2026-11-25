param(
    [int[]]$Rates = @(250, 500, 1000, 2000, 3000),
    [int]$DurationSeconds = 30
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

$namespace = 'geopulse'
$k6Image = 'grafana/k6:2.4.0'
$jobName = 'k6-ping-load'

$ErrorActionPreference = 'Continue'
& docker image inspect $k6Image 2>$null | Out-Null
$haveImage = $LASTEXITCODE -eq 0
$ErrorActionPreference = 'Stop'
if (-not $haveImage) {
    & docker pull $k6Image
    if ($LASTEXITCODE -ne 0) { throw 'Could not pull the k6 image.' }
}
$scriptFile = "ping-load.js=$(Join-Path $PSScriptRoot 'ping-load.js')"
& $kubectl -n $namespace create configmap k6-ping-load "--from-file=$scriptFile" --dry-run=client -o yaml | & $kubectl apply -f -
if ($LASTEXITCODE -ne 0) { throw 'Could not store the k6 script.' }

function Get-PodName([string]$label) {
    $name = & $kubectl -n $namespace get pod -l $label -o jsonpath='{.items[0].metadata.name}'
    if ($LASTEXITCODE -ne 0 -or -not $name) { throw "No pod found for $label." }
    return $name
}

# CPU time used by a pod's container, in seconds, from its cgroup counters.
function Get-CpuSeconds([string]$pod) {
    $stat = & $kubectl -n $namespace exec $pod -- cat /sys/fs/cgroup/cpu.stat
    if ($LASTEXITCODE -ne 0) { throw "Could not read CPU use of $pod." }
    $line = $stat | Where-Object { $_ -like 'usage_usec *' }
    return [double](($line -split ' ')[1]) / 1e6
}

function Get-PingCount {
    $sql = 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT count(*) FROM pings"'
    $count = $sql | & $kubectl -n $namespace exec -i $dbQueryPod -- sh
    if ($LASTEXITCODE -ne 0) { throw 'Could not count stored pings.' }
    return [int64]("$count".Trim())
}

function Get-DatabaseCpuSeconds {
    $total = 0.0
    foreach ($pod in $dbPods) {
        $total += Get-CpuSeconds $pod
    }
    return $total
}

$apiDeploymentJson = & $kubectl -n $namespace get deployment geopulse-api -o json
if ($LASTEXITCODE -ne 0) { throw 'Could not inspect the API database configuration.' }
$apiDeployment = $apiDeploymentJson | ConvertFrom-Json
$apiContainer = $apiDeployment.spec.template.spec.containers | Where-Object { $_.name -eq 'api' }
$databaseSecret = ($apiContainer.env | Where-Object { $_.name -eq 'DATABASE_URL' }).valueFrom.secretKeyRef.name
if ($databaseSecret -eq 'geopulse-database') {
    $dbQueryPod = 'postgres-0'
    $dbPods = @('postgres-0')
} elseif ($databaseSecret -eq 'geopulse-citus-database') {
    $dbQueryPod = 'geopulse-citus-coordinator-0'
    $dbPods = @('geopulse-citus-coordinator-0', 'geopulse-citus-workers-0', 'geopulse-citus-workers-1')
} else {
    throw 'The API is not using a recognized PostgreSQL or Citus database secret.'
}

$apiPod = Get-PodName 'app=geopulse-api'
$results = @()

foreach ($rate in $Rates) {
    & $kubectl -n $namespace delete job $jobName --ignore-not-found --wait=true | Out-Null

    $countBefore = Get-PingCount
    $testRunId = [guid]::NewGuid().ToString('N').Substring(0, 8)
    Write-Host "Load test run ID: $testRunId"

    $job = @"
apiVersion: batch/v1
kind: Job
metadata:
  name: $jobName
  namespace: $namespace
spec:
  backoffLimit: 0
  ttlSecondsAfterFinished: 600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: k6
          image: $k6Image
          imagePullPolicy: IfNotPresent
          args: ["run", "/scripts/ping-load.js"]
          env:
            - name: RATE
              value: "$rate"
            - name: DURATION
              value: "${DurationSeconds}s"
            - name: TEST_RUN_ID
              value: "$testRunId"
          resources:
            requests:
              cpu: 500m
              memory: 256Mi
            limits:
              memory: 1Gi
          volumeMounts:
            - name: script
              mountPath: /scripts
      volumes:
        - name: script
          configMap:
            name: k6-ping-load
"@
    $job | & $kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw 'Could not start the load job.' }

    for ($i = 0; $i -lt 60; $i++) {
        $phase = & $kubectl -n $namespace get pod -l "job-name=$jobName" -o jsonpath='{.items[0].status.phase}' 2>$null
        if ($phase -eq 'Running' -or $phase -eq 'Succeeded' -or $phase -eq 'Failed') { break }
        Start-Sleep -Seconds 2
    }

    $apiCpuBefore = Get-CpuSeconds $apiPod
    $dbCpuBefore = Get-DatabaseCpuSeconds
    $clock = [System.Diagnostics.Stopwatch]::StartNew()

    $finished = $false
    for ($i = 0; $i -lt (($DurationSeconds + 120) / 2); $i++) {
        $succeeded = & $kubectl -n $namespace get job $jobName -o jsonpath='{.status.succeeded}'
        $failed = & $kubectl -n $namespace get job $jobName -o jsonpath='{.status.failed}'
        if ($succeeded -eq '1' -or $failed) { $finished = $true; break }
        Start-Sleep -Seconds 2
    }
    $clock.Stop()
    $apiCpuAfter = Get-CpuSeconds $apiPod
    $dbCpuAfter = Get-DatabaseCpuSeconds
    if (-not $finished) { throw "The load job for rate $rate did not finish." }

    $log = & $kubectl -n $namespace logs "job/$jobName"
    $line = $log | Where-Object { $_ -like 'RESULT *' } | Select-Object -Last 1
    if (-not $line) { throw "No result was printed for rate $rate." }
    $r = ($line -replace '^RESULT ', '') | ConvertFrom-Json

    $countAfter = Get-PingCount
    $seconds = $clock.Elapsed.TotalSeconds
    $row = [pscustomobject]@{
        Target = $rate
        Requests = $r.requests
        Accepted = $r.accepted
        Achieved = $r.achieved_per_s
        FailedPct = [math]::Round($r.failed_ratio * 100, 2)
        Dropped = $r.dropped
        P50ms = $r.p50_ms
        P95ms = $r.p95_ms
        P99ms = $r.p99_ms
        Stored = $countAfter - $countBefore
        ApiCores = [math]::Round(($apiCpuAfter - $apiCpuBefore) / $seconds, 2)
        DbCores = [math]::Round(($dbCpuAfter - $dbCpuBefore) / $seconds, 2)
    }
    $results += $row
    $row | ConvertTo-Csv -NoTypeInformation | Select-Object -Last 1 | Write-Host
    Start-Sleep -Seconds 10
}

$results | ConvertTo-Csv -NoTypeInformation
