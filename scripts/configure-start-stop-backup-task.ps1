[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ProjectDir
)

$ErrorActionPreference = "Stop"
$resolvedProject = [System.IO.Path]::GetFullPath($ProjectDir)
$wrapper = Join-Path $resolvedProject "scripts\docker-compose.ps1"
if (-not (Test-Path -LiteralPath $wrapper -PathType Leaf)) {
    throw "Docker Compose wrapper is missing."
}

$initializationOutput = & powershell.exe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $wrapper `
    backup-status `
    --state never_run `
    --exit-code 0 `
    --if-missing
$initializationExitCode = $LASTEXITCODE
if ($initializationExitCode -ne 0) {
    throw "Could not initialize the scheduled backup status volume."
}

$composeOutput = & powershell.exe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $wrapper `
    up `
    --detach `
    --no-deps `
    backup-scheduler
$composeExitCode = $LASTEXITCODE
if ($composeExitCode -ne 0) {
    throw "Could not start the Docker backup scheduler."
}

[ordered]@{
    scheduler = "docker"
    container = "start-stop-analysis-backup-scheduler"
    state = "started"
    schedule = "every 4 weeks on Sunday at 03:00"
    first_due = "2026-08-30 03:00 Asia/Shanghai"
    catch_up_missed_runs = $false
    full_backup_blocks_web = $false
    status_storage = "docker_volume"
} | ConvertTo-Json -Compress
