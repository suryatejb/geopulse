param(
    [int]$TimeoutSeconds = 360,
    [double]$MinimumFreeGiB = 2.25
)

$ErrorActionPreference = 'Stop'

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$kubeconfig = Join-Path $repoRoot '.geopulse\kubeconfig'
$namespace = 'geopulse'
$topic = 'pings-phase3-recovery-test'
$sparkApplication = 'geopulse-phase3-recovery-test'
$redisPod = 'geopulse-phase3-recovery-redis-0'
$redisKeyPrefix = 'geopulse:stats:'
$cell = '882a107289fffff'
$image = 'geopulse-phase3-spark:0.2.1'
$testResources = @(
    'spark-test-application.yaml',
    'recovery-redis.yaml',
    'recovery-topic.yaml'
)
$applyResources = @(
    'recovery-topic.yaml',
    'recovery-redis.yaml',
    'recovery-checkpoint-pvc.yaml',
    'spark-test-application.yaml'
)

if (-not (Test-Path -LiteralPath $kubeconfig)) {
    throw 'The GeoPulse kubeconfig is missing.'
}
$env:KUBECONFIG = $kubeconfig
$freeMemoryGiB = (Get-CimInstance -ClassName Win32_OperatingSystem).FreePhysicalMemory / 1MB
if ($freeMemoryGiB -lt $MinimumFreeGiB) {
    throw ("Only {0:N2} GiB of host memory is free; need at least {1:N2} GiB " +
        'before starting the Spark recovery test.') -f $freeMemoryGiB, $MinimumFreeGiB
}

function Invoke-Kubectl {
    param([string[]]$Arguments)

    $output = & kubectl @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "kubectl failed: $($Arguments -join ' ')"
    }
    return $output
}

function Wait-SparkRunning {
    param([DateTime]$Deadline)

    while ([DateTime]::UtcNow -lt $Deadline) {
        $state = Invoke-Kubectl @(
            '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
            '-o', 'jsonpath={.status.applicationState.state}'
        )
        if ($state -eq 'RUNNING') {
            return
        }
        if ($state -eq 'FAILED' -or $state -eq 'FAILING' -or $state -eq 'SUBMISSION_FAILED') {
            Invoke-Kubectl @('-n', $namespace, 'describe', 'sparkapplication', $sparkApplication)
            $errorMessage = Invoke-Kubectl @(
                '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
                '-o', 'jsonpath={.status.applicationState.errorMessage}'
            )
            $driverPod = Invoke-Kubectl @(
                '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
                '-o', 'jsonpath={.status.driverInfo.podName}'
            )
            if ($driverPod) {
                $existingPod = & kubectl -n $namespace get pod $driverPod -o name 2>$null
                if ($LASTEXITCODE -eq 0 -and $existingPod) {
                    Invoke-Kubectl @('-n', $namespace, 'logs', $driverPod, '--tail=100')
                }
            }
            throw "SparkApplication entered state '$state': $errorMessage"
        }
        Start-Sleep -Seconds 5
    }
    throw "SparkApplication did not reach RUNNING within $TimeoutSeconds seconds."
}

function Publish-TestPings {
    param([long]$MinuteStart)

    $producerName = 'geopulse-phase3-recovery-producer'
    Invoke-Kubectl @(
        'run', $producerName,
        "--namespace=$namespace",
        '--restart=Never',
        '--rm',
        '-i',
        "--image=$image",
        '--image-pull-policy=IfNotPresent',
        "--env=KAFKA_BOOTSTRAP_SERVERS=geopulse-kafka-bootstrap.kafka.svc:9092",
        "--env=KAFKA_TOPIC=$topic",
        "--env=PHASE3_TEST_MINUTE_START=$MinuteStart",
        '--',
        'python', '-m', 'streaming.producer_test'
    )
}

function Get-RedisField {
    param(
        [string]$Key,
        [string]$Field
    )

    $value = Invoke-Kubectl @(
        '-n', $namespace, 'exec', $redisPod, '--', 'redis-cli', 'HGET', $Key, $Field
    )
    return [string]$value
}

function Wait-ExpectedStats {
    param(
        [long]$MinuteStart,
        [DateTime]$Deadline
    )

    $key = "$redisKeyPrefix${MinuteStart}:$cell"
    while ([DateTime]::UtcNow -lt $Deadline) {
        $state = Invoke-Kubectl @(
            '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
            '-o', 'jsonpath={.status.applicationState.state}'
        )
        if ($state -eq 'FAILED' -or $state -eq 'FAILING' -or
            $state -eq 'SUBMISSION_FAILED' -or $state -eq 'COMPLETED') {
            $errorMessage = Invoke-Kubectl @(
                '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
                '-o', 'jsonpath={.status.applicationState.errorMessage}'
            )
            Invoke-Kubectl @(
                '-n', $namespace, 'describe', 'sparkapplication', $sparkApplication
            ) | Out-Host
            $driverPod = Invoke-Kubectl @(
                '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
                '-o', 'jsonpath={.status.driverInfo.podName}'
            )
            if ($driverPod) {
                $existingPod = & kubectl -n $namespace get pod $driverPod -o name 2>$null
                if ($LASTEXITCODE -eq 0 -and $existingPod) {
                    Invoke-Kubectl @('-n', $namespace, 'logs', $driverPod, '--tail=100') |
                        Out-Host
                }
            }
            throw "SparkApplication entered state '$state': $errorMessage"
        }
        $count = Get-RedisField -Key $key -Field 'vehicle_count'
        $sum = Get-RedisField -Key $key -Field 'speed_sum'
        if ($count -eq '2' -and [double]$sum -eq 70.0) {
            return $key
        }
        Start-Sleep -Seconds 5
    }
    throw "Expected two vehicles and speed sum 70 in Redis key '$key'."
}

function Test-CheckpointPresent {
    $driverPod = Invoke-Kubectl @(
        '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
        '-o', 'jsonpath={.status.driverInfo.podName}'
    )
    if (-not $driverPod) {
        throw 'SparkApplication has no driver pod in its status.'
    }
    Invoke-Kubectl @('-n', $namespace, 'exec', $driverPod, '--', 'test', '-f', '/checkpoint/metadata')
}

function Wait-CheckpointCommitted {
    param([DateTime]$Deadline)

    $driverPod = Invoke-Kubectl @(
        '-n', $namespace, 'get', 'sparkapplication', $sparkApplication,
        '-o', 'jsonpath={.status.driverInfo.podName}'
    )
    if (-not $driverPod) {
        throw 'SparkApplication has no driver pod for checkpoint verification.'
    }

    $checkCommit = (
        'import pathlib; ' +
        'p=pathlib.Path(chr(47)+bytes([99,104,101,99,107,112,111,105,110,116]).decode()); ' +
        'offsets=sorted(int(f.name) for f in (p/bytes([111,102,102,115,101,116,115]).decode()).glob(chr(42)) if f.name.isdigit()); ' +
        'commits={int(f.name) for f in (p/bytes([99,111,109,109,105,116,115]).decode()).glob(chr(42)) if f.name.isdigit()}; ' +
        'print(bool(offsets) and offsets[-1] in commits)'
    )

    while ([DateTime]::UtcNow -lt $Deadline) {
        $result = Invoke-Kubectl @(
            '-n', $namespace, 'exec', $driverPod, '--',
            'python', '-c', $checkCommit
        )
        if ($result -eq 'True') {
            return
        }
        Start-Sleep -Seconds 3
    }
    throw 'Spark did not commit its latest Kafka offsets to the checkpoint.'
}

function Remove-RecoveryResources {
    $failures = @()
    foreach ($manifest in $testResources) {
        $path = Join-Path $PSScriptRoot $manifest
        & kubectl delete -f $path --ignore-not-found=true --wait=true 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            $failures += "Failed to remove $manifest"
        }
    }
    & kubectl -n $namespace delete pvc data-geopulse-phase3-recovery-redis-0 `
        --ignore-not-found=true --wait=true 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $failures += 'Failed to remove the recovery Redis PVC'
    }
    & kubectl -n $namespace delete pvc geopulse-phase3-recovery-checkpoint `
        --ignore-not-found=true --wait=true 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        $failures += 'Failed to remove the recovery checkpoint PVC'
    }
    if ($failures.Count -gt 0) {
        throw ($failures -join '; ')
    }
}

if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) {
    throw 'kubectl was not found on PATH.'
}
$operator = Invoke-Kubectl @(
    '-n', 'spark-operator', 'get', 'deployment', 'geopulse-spark-operator-controller'
)
if (-not $operator) {
    throw 'Deploy the Phase 3 Spark Operator first.'
}
$redis = Invoke-Kubectl @(
    '-n', $namespace, 'get', 'statefulset', 'geopulse-phase3-redis'
)
if (-not $redis) {
    throw 'Deploy the persistent Phase 3 Redis instance first.'
}
$liveApplication = Invoke-Kubectl @(
    '-n', $namespace, 'get', 'sparkapplication', 'geopulse-live-statistics',
    '--ignore-not-found', '-o', 'name'
)
if ($liveApplication) {
    $liveState = Invoke-Kubectl @(
        '-n', $namespace, 'get', 'sparkapplication', 'geopulse-live-statistics',
        '-o', 'jsonpath={.status.applicationState.state}'
    )
    if ($liveState -ne 'FAILED' -and $liveState -ne 'COMPLETED') {
        throw ("The production SparkApplication is in state '$liveState'. Stop it before " +
            'running the recovery test to avoid concurrent Spark workloads. Use ' +
            'kubectl delete -f infra\kubernetes\phase3\spark-application.yaml --wait=true, then ' +
            'rerun deploy.ps1 after the test.')
    }
}

$testMinuteStart = [long]([Math]::Floor(
    [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() / 60
) * 60) - 180
$testKey = "$redisKeyPrefix${testMinuteStart}:$cell"

try {
    Remove-RecoveryResources
    foreach ($manifest in $applyResources) {
        Invoke-Kubectl @('apply', '-f', (Join-Path $PSScriptRoot $manifest))
    }
    Invoke-Kubectl @(
        '-n', 'kafka', 'wait', "kafkatopic/$topic",
        '--for=condition=Ready', '--timeout=120s'
    )
    Invoke-Kubectl @(
        '-n', $namespace, 'rollout', 'status',
        'statefulset/geopulse-phase3-recovery-redis', '--timeout=180s'
    )
    Wait-SparkRunning -Deadline ([DateTime]::UtcNow.AddSeconds($TimeoutSeconds))
    Publish-TestPings -MinuteStart $testMinuteStart
    $testKey = Wait-ExpectedStats -MinuteStart $testMinuteStart `
        -Deadline ([DateTime]::UtcNow.AddSeconds($TimeoutSeconds))
    Test-CheckpointPresent
    Wait-CheckpointCommitted -Deadline ([DateTime]::UtcNow.AddSeconds(120))

    Start-Sleep -Seconds 2
    Invoke-Kubectl @('-n', $namespace, 'delete', 'pod', $redisPod, '--wait=true')
    $redisReady = $false
    $redisDeadline = [DateTime]::UtcNow.AddSeconds(180)
    while ([DateTime]::UtcNow -lt $redisDeadline) {
        $pod = & kubectl -n $namespace get pod $redisPod -o name 2>$null
        if ($LASTEXITCODE -eq 0 -and $pod) {
            & kubectl -n $namespace wait --for=condition=Ready "pod/$redisPod" --timeout=15s
            if ($LASTEXITCODE -eq 0) {
                $redisReady = $true
                break
            }
        }
        Start-Sleep -Seconds 3
    }
    if (-not $redisReady) {
        throw 'Recovery Redis did not return after its pod restart.'
    }
    if ((Get-RedisField -Key $testKey -Field 'vehicle_count') -ne '2' -or
        [double](Get-RedisField -Key $testKey -Field 'speed_sum') -ne 70.0) {
        throw 'Redis statistics did not survive the Redis pod restart.'
    }

    Invoke-Kubectl @(
        'delete', '-f', (Join-Path $PSScriptRoot 'spark-test-application.yaml'),
        '--wait=true'
    )
    Invoke-Kubectl @(
        'apply', '-f', (Join-Path $PSScriptRoot 'spark-test-application.yaml')
    )
    Wait-SparkRunning -Deadline ([DateTime]::UtcNow.AddSeconds($TimeoutSeconds))
    Test-CheckpointPresent
    if ((Get-RedisField -Key $testKey -Field 'vehicle_count') -ne '2' -or
        [double](Get-RedisField -Key $testKey -Field 'speed_sum') -ne 70.0) {
        throw 'Existing statistics changed after the Spark checkpoint restart.'
    }

    $nextMinuteStart = [long]([Math]::Floor(
        [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() / 60
    ) * 60)
    if ($nextMinuteStart -le $testMinuteStart) {
        $nextMinuteStart = $testMinuteStart + 60
    }
    Publish-TestPings -MinuteStart $nextMinuteStart
    $nextKey = Wait-ExpectedStats -MinuteStart $nextMinuteStart `
        -Deadline ([DateTime]::UtcNow.AddSeconds($TimeoutSeconds))
    Wait-CheckpointCommitted -Deadline ([DateTime]::UtcNow.AddSeconds(120))
    Write-Output "Redis pod restart preserved $testKey."
    Write-Output "Spark checkpoint restart resumed ingestion into $nextKey."
} finally {
    Remove-RecoveryResources
}
