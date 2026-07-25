param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8788,
    [switch]$NoBrowser,
    [switch]$AcceptanceOnly,
    [switch]$AllowInstrumentControl
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$target = $PSScriptRoot
$python = Join-Path $target 'runtime\python.exe'
$app = Join-Path $target 'app.py'
$config = Join-Path $target 'config.json'
$logs = Join-Path $target 'logs'
$state = Join-Path $target 'state'
$stdout = Join-Path $logs ("stage-c-desktop-{0}.stdout.log" -f $Port)
$stderr = Join-Path $logs ("stage-c-desktop-{0}.stderr.log" -f $Port)
$database = Join-Path $state ("stage-c-desktop-{0}.sqlite3" -f $Port)
$pidFile = Join-Path $state ("stage-c-desktop-{0}.pid.json" -f $Port)
$uri = "http://127.0.0.1:$Port"

function Stop-CreatedPlatform {
    param(
        [System.Diagnostics.Process]$Process,
        [bool]$Created
    )
    if ($Created -and $null -ne $Process -and -not $Process.HasExited) {
        Stop-Process -Id $Process.Id -Force -ErrorAction SilentlyContinue
        Start-Sleep -Milliseconds 300
    }
    if ($Created) {
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    }
}

foreach ($path in @($python, $app, $config)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required Stage C file is missing: $path"
    }
}

$launcherSession = (Get-Process -Id $PID -ErrorAction Stop).SessionId
$explorerSessions = @(
    Get-Process -Name 'explorer' -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty SessionId -Unique |
        Sort-Object
)
if ($launcherSession -le 0 -or $launcherSession -notin $explorerSessions) {
    throw (
        "Stage C must be started from the currently logged-in Windows desktop. " +
        "Launcher session: $launcherSession; Explorer sessions: " +
        "[$($explorerSessions -join ', ')]."
    )
}

New-Item -ItemType Directory -Path $logs -Force | Out-Null
New-Item -ItemType Directory -Path $state -Force | Out-Null

$listeners = @(
    Get-NetTCPConnection -LocalAddress '127.0.0.1' -LocalPort $Port `
        -State Listen -ErrorAction SilentlyContinue
)
$created = $false
$platformProcess = $null

if ($listeners.Count -eq 0) {
    $arguments = @(
        ('"{0}"' -f $app),
        '--config',
        ('"{0}"' -f $config),
        '--database',
        ('"{0}"' -f $database),
        '--port',
        [string]$Port,
        '--no-watch'
    )
    $platformProcess = Start-Process -FilePath $python `
        -ArgumentList $arguments `
        -WorkingDirectory $target `
        -RedirectStandardOutput $stdout `
        -RedirectStandardError $stderr `
        -WindowStyle Hidden `
        -PassThru
    $created = $true
}
elseif ($listeners.Count -eq 1) {
    $ownerId = $listeners[0].OwningProcess
    $owner = Get-CimInstance Win32_Process -Filter "ProcessId=$ownerId" `
        -ErrorAction Stop
    if (
        $owner.ExecutablePath -ine $python -or
        $owner.CommandLine -notlike "*$app*" -or
        $owner.CommandLine -notlike "*--port $Port*"
    ) {
        throw "Port $Port is already used by another application."
    }
    $platformProcess = Get-Process -Id $ownerId -ErrorAction Stop
}
else {
    throw "Multiple processes are listening on Stage C port $Port."
}

$status = $null
$preflight = $null
for ($attempt = 0; $attempt -lt 40; $attempt++) {
    if ($created -and $platformProcess.HasExited) {
        $errorText = [string](
            Get-Content -LiteralPath $stderr -Raw -ErrorAction SilentlyContinue
        )
        throw "Stage C exited during desktop startup. $errorText"
    }
    try {
        $status = Invoke-RestMethod -Uri "$uri/api/status" `
            -Method Get -TimeoutSec 2
        $preflight = Invoke-RestMethod -Uri "$uri/api/control/preflight" `
            -Method Get -TimeoutSec 2
        break
    }
    catch {
        Start-Sleep -Milliseconds 250
    }
}

if ($null -eq $status -or $null -eq $preflight) {
    Stop-CreatedPlatform -Process $platformProcess -Created $created
    throw "Stage C desktop health check timed out."
}
if (
    -not $status.loopback_only -or
    $status.serial_access -or
    $status.control_stage -ne 'ocp_60s_preflight'
) {
    Stop-CreatedPlatform -Process $platformProcess -Created $created
    throw "Stage C desktop safety status is not valid."
}
if ($status.instrument_control -and -not $AllowInstrumentControl) {
    Stop-CreatedPlatform -Process $platformProcess -Created $created
    throw (
        "Instrument control is enabled. The normal desktop launcher only opens " +
        "the locked monitor. A fresh authorized run requires the explicit " +
        "-AllowInstrumentControl switch."
    )
}

$sessionCheck = $preflight.checks |
    Where-Object { $_.id -eq 'interactive_desktop_session' } |
    Select-Object -First 1
if ($null -eq $sessionCheck -or $sessionCheck.status -ne 'passed') {
    Stop-CreatedPlatform -Process $platformProcess -Created $created
    throw "Stage C preflight did not confirm the current Windows desktop session."
}
if ($preflight.writes_performed -or $preflight.instrument_started) {
    Stop-CreatedPlatform -Process $platformProcess -Created $created
    throw "Stage C read-only preflight unexpectedly reported a write or launch."
}

if ($created) {
    [ordered]@{
        process_id = $platformProcess.Id
        port = $Port
        target = $target
        launcher_session_id = $launcherSession
        started_utc = (Get-Date).ToUniversalTime().ToString('o')
    } | ConvertTo-Json -Depth 3 |
        Set-Content -LiteralPath $pidFile -Encoding UTF8
}

if ($AcceptanceOnly) {
    if ($status.instrument_control) {
        Stop-CreatedPlatform -Process $platformProcess -Created $created
        throw "AcceptanceOnly refuses to stop a control-enabled platform."
    }
    Stop-CreatedPlatform -Process $platformProcess -Created $created
    [Console]::Out.WriteLine(
        ([ordered]@{
            status = 'accepted'
            started_and_stopped = $created
            port = $Port
            launcher_session_id = $launcherSession
            explorer_session_ids = $explorerSessions
            interactive_desktop_session = $sessionCheck.status
            instrument_control = $status.instrument_control
            instrument_started = $preflight.instrument_started
            writes_performed = $preflight.writes_performed
        } | ConvertTo-Json -Depth 4)
    )
    exit 0
}

if (-not $NoBrowser) {
    Start-Process "$uri/monitor"
}
[Console]::Out.WriteLine(
    ([ordered]@{
        status = 'running'
        url = "$uri/monitor"
        process_id = $platformProcess.Id
        port = $Port
        launcher_session_id = $launcherSession
        explorer_session_ids = $explorerSessions
        instrument_control = $status.instrument_control
        instrument_started = $preflight.instrument_started
        writes_performed = $preflight.writes_performed
    } | ConvertTo-Json -Depth 4)
)
