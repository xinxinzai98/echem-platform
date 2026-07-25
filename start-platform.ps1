param(
    [switch]$NoBrowser,
    [switch]$AcceptanceOnly
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$target = Split-Path -Parent $MyInvocation.MyCommand.Path
$python = Join-Path $target 'runtime\python.exe'
$app = Join-Path $target 'app.py'
$config = Join-Path $target 'config.json'
$logs = Join-Path $target 'logs'
$state = Join-Path $target 'state'
$stdout = Join-Path $logs 'platform.stdout.log'
$stderr = Join-Path $logs 'platform.stderr.log'
$pidFile = Join-Path $state 'server.pid'
$uri = 'http://127.0.0.1:8787'

foreach ($path in @($python, $app, $config)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required platform file is missing: $path"
    }
}
New-Item -ItemType Directory -Path $logs -Force | Out-Null
New-Item -ItemType Directory -Path $state -Force | Out-Null

$listeners = @(
    Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort 8787 -State Listen `
        -ErrorAction SilentlyContinue
)
$created = $false
$platformProcess = $null

if ($listeners.Count -gt 0) {
    if ($listeners.Count -ne 1) {
        throw "Multiple listeners are using 127.0.0.1:8787."
    }
    $ownerId = $listeners[0].OwningProcess
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId"
    if ($owner.ExecutablePath -ine $python -or $owner.CommandLine -notmatch 'app\.py') {
        throw "Port 8787 is already used by another application."
    }
    $platformProcess = Get-Process -Id $ownerId
}
else {
    $platformProcess = Start-Process -FilePath $python `
        -ArgumentList @($app, '--config', $config) `
        -WorkingDirectory $target `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
    $created = $true
    Set-Content -LiteralPath $pidFile -Value $platformProcess.Id -Encoding ASCII
}

$healthy = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if ($created -and $platformProcess.HasExited) {
        $errorText = Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue
        throw "Platform exited during startup. $errorText"
    }
    try {
        $status = Invoke-RestMethod -Uri "$uri/api/status" -Method Get -TimeoutSec 2
        if ($status.loopback_only -and -not $status.serial_access -and
            $status.control_stage -eq 'ocp_60s_preflight') {
            $healthy = $true
            break
        }
    }
    catch {
        Start-Sleep -Milliseconds 250
    }
}

if (-not $healthy) {
    if ($created -and -not $platformProcess.HasExited) {
        Stop-Process -Id $platformProcess.Id -Force
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    throw "Platform health check timed out."
}

if ($AcceptanceOnly) {
    if ($created -and -not $platformProcess.HasExited) {
        Stop-Process -Id $platformProcess.Id -Force
        Start-Sleep -Milliseconds 300
    }
    Remove-Item -LiteralPath $pidFile -ErrorAction SilentlyContinue
    [Console]::Out.WriteLine(
        ([ordered]@{
            Status = 'accepted'
            Http = 200
            StartedAndStopped = $created
            LoopbackOnly = $status.loopback_only
            SerialAccess = $status.serial_access
            InstrumentControl = $status.instrument_control
            ControlStage = $status.control_stage
        } | ConvertTo-Json -Depth 3)
    )
    exit 0
}

Set-Content -LiteralPath $pidFile -Value $platformProcess.Id -Encoding ASCII
if (-not $NoBrowser) {
    Start-Process $uri
}
[Console]::Out.WriteLine("EchemPlatform is running at $uri")
