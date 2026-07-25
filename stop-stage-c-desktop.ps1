param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8788
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$target = $PSScriptRoot
$python = Join-Path $target 'runtime\python.exe'
$app = Join-Path $target 'app.py'
$state = Join-Path $target 'state'
$pidFile = Join-Path $state ("stage-c-desktop-{0}.pid.json" -f $Port)
$uri = "http://127.0.0.1:$Port"

$listeners = @(
    Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port `
        -State Listen -ErrorAction SilentlyContinue
)
if ($listeners.Count -eq 0) {
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    [Console]::Out.WriteLine(
        ([ordered]@{
            status = 'already_stopped'
            port = $Port
        } | ConvertTo-Json -Depth 2)
    )
    exit 0
}
if ($listeners.Count -ne 1) {
    throw "Multiple processes are listening on Stage C port $Port."
}

$ownerId = $listeners[0].OwningProcess
$owner = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" `
    -ErrorAction Stop
if (
    $owner.ExecutablePath -ine $python -or
    $owner.CommandLine -notlike "*$app*" -or
    $owner.CommandLine -notlike "*--port $Port*"
) {
    throw "Port $Port is not owned by this Stage C deployment."
}

try {
    $status = Invoke-RestMethod -Uri "$uri/api/status" -Method Get -TimeoutSec 2
}
catch {
    throw (
        "Stage C status cannot be verified. The launcher will not stop an " +
        "unverified process."
    )
}

if ($status.instrument_control) {
    $runs = @(
        Invoke-RestMethod -Uri "$uri/api/control/runs?limit=50" `
            -Method Get -TimeoutSec 2
    )
    $activeRuns = @(
        $runs |
            Where-Object {
                $_.status -in @('starting', 'running', 'stop_requested')
            }
    )
    if ($activeRuns.Count -gt 0) {
        throw (
            "Platform stop blocked: an instrument run is active. Stop safely " +
            "inside CHI and finish the run review first."
        )
    }
}

Stop-Process -Id $ownerId -Force
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    Start-Sleep -Milliseconds 150
    $remaining = @(
        Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port `
            -State Listen -ErrorAction SilentlyContinue
    )
    if ($remaining.Count -eq 0) {
        break
    }
}
if ($remaining.Count -ne 0) {
    throw "Stage C platform did not release port $Port."
}

Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
[Console]::Out.WriteLine(
    ([ordered]@{
        status = 'stopped'
        process_id = $ownerId
        port = $Port
        instrument_control = $status.instrument_control
    } | ConvertTo-Json -Depth 3)
)
