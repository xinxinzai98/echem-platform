[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ComposeArguments
)

$ErrorActionPreference = "Stop"
$projectDir = Split-Path -Parent $PSScriptRoot
$stateRoot = Join-Path $projectDir "state\docker"

@(
    $stateRoot,
    (Join-Path $stateRoot "published"),
    (Join-Path $stateRoot "backups"),
    (Join-Path $stateRoot "secrets"),
    (Join-Path $stateRoot "import")
) | ForEach-Object {
    if (-not (Test-Path -LiteralPath $_)) {
        New-Item -ItemType Directory -Path $_ | Out-Null
    }
}

$docker = Get-Command docker.exe -ErrorAction Stop

function Get-DotEnvValue {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$DefaultValue
    )
    $EnvironmentPath = Join-Path $projectDir ".env"
    if (-not (Test-Path -LiteralPath $EnvironmentPath -PathType Leaf)) {
        return $DefaultValue
    }
    foreach ($Line in (Get-Content -LiteralPath $EnvironmentPath)) {
        if ($Line -match "^\s*$([regex]::Escape($Name))\s*=\s*(.*?)\s*$") {
            $Value = [string]$Matches[1]
            if (
                $Value.Length -ge 2 -and
                (($Value.StartsWith('"') -and $Value.EndsWith('"')) -or
                 ($Value.StartsWith("'") -and $Value.EndsWith("'")))
            ) {
                $Value = $Value.Substring(1, $Value.Length - 2)
            }
            return $Value
        }
    }
    return $DefaultValue
}

$BackupStatusVolume = Get-DotEnvValue `
    -Name "ECHEM_BACKUP_STATUS_VOLUME_NAME" `
    -DefaultValue "start-stop-analysis-backup-status"
& $docker.Source volume inspect $BackupStatusVolume *> $null
if ($LASTEXITCODE -ne 0) {
    & $docker.Source volume create $BackupStatusVolume | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the scheduled backup status volume."
    }
}

if ($ComposeArguments.Count -gt 0 -and $ComposeArguments[0] -eq "check") {
    $ImageRepository = Get-DotEnvValue -Name "ECHEM_IMAGE_REPOSITORY" -DefaultValue "start-stop-analysis"
    $ImageTag = Get-DotEnvValue -Name "ECHEM_IMAGE_TAG" -DefaultValue "0.6.0-dev.7.2-windows"
    $DatabaseVolume = Get-DotEnvValue -Name "ECHEM_DATABASE_VOLUME_NAME" -DefaultValue "start-stop-analysis-database"
    $Image = "{0}:{1}" -f $ImageRepository, $ImageTag
    & $docker.Source run `
        --rm `
        --network none `
        --read-only `
        --cap-drop ALL `
        --security-opt no-new-privileges:true `
        --tmpfs /tmp:size=64m,mode=1777 `
        --mount "type=volume,source=$DatabaseVolume,target=/app/state/database,readonly" `
        --entrypoint python3 `
        $Image `
        /app/scripts/create_start_stop_backup.py `
        --database /app/state/database/start-stop.sqlite3 `
        --check-live `
        --compact
    if ($LASTEXITCODE -ne 0) {
        throw "启停数据库快速一致性检查失败，退出码：$LASTEXITCODE"
    }
    exit 0
}

if ($ComposeArguments.Count -gt 0 -and $ComposeArguments[0] -eq "backup-status") {
    $ImageRepository = Get-DotEnvValue -Name "ECHEM_IMAGE_REPOSITORY" -DefaultValue "start-stop-analysis"
    $ImageTag = Get-DotEnvValue -Name "ECHEM_IMAGE_TAG" -DefaultValue "0.6.0-dev.7.2-windows"
    $Image = "{0}:{1}" -f $ImageRepository, $ImageTag
    $StatusArguments = @(
        "run",
        "--rm",
        "--network", "none",
        "--read-only",
        "--user", "0:0",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp:size=16m,mode=1777",
        "--mount", "type=volume,source=$BackupStatusVolume,target=/app/state/backup-status",
        "--entrypoint", "python3",
        $Image,
        "/app/scripts/write_start_stop_backup_status.py",
        "--status-file", "/app/state/backup-status/scheduled-backup-status.json"
    )
    if ($ComposeArguments.Count -gt 1) {
        $StatusArguments += $ComposeArguments[1..($ComposeArguments.Count - 1)]
    }
    & $docker.Source @StatusArguments
    if ($LASTEXITCODE -ne 0) {
        throw "Scheduled backup status update failed with exit code $LASTEXITCODE"
    }
    exit 0
}

if ($ComposeArguments.Count -gt 0 -and $ComposeArguments[0] -eq "backup") {
    $ImageRepository = Get-DotEnvValue -Name "ECHEM_IMAGE_REPOSITORY" -DefaultValue "start-stop-analysis"
    $ImageTag = Get-DotEnvValue -Name "ECHEM_IMAGE_TAG" -DefaultValue "0.6.0-dev.7.2-windows"
    $DatabaseVolume = Get-DotEnvValue -Name "ECHEM_DATABASE_VOLUME_NAME" -DefaultValue "start-stop-analysis-database"
    $BackupCpuLimit = Get-DotEnvValue -Name "ECHEM_BACKUP_CPU_LIMIT" -DefaultValue "1.0"
    $ConfiguredBackupDir = Get-DotEnvValue -Name "ECHEM_BACKUP_DIR" -DefaultValue "./state/docker/backups"
    $BackupDir = if ([System.IO.Path]::IsPathRooted($ConfiguredBackupDir)) {
        [System.IO.Path]::GetFullPath($ConfiguredBackupDir)
    } else {
        [System.IO.Path]::GetFullPath((Join-Path $projectDir $ConfiguredBackupDir))
    }
    if (-not (Test-Path -LiteralPath $BackupDir)) {
        New-Item -ItemType Directory -Path $BackupDir | Out-Null
    }
    $Image = "{0}:{1}" -f $ImageRepository, $ImageTag
    $BackupArguments = @(
        "run",
        "--rm",
        "--name", "start-stop-analysis-full-backup",
        "--network", "none",
        "--cpus", $BackupCpuLimit,
        "--read-only",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--tmpfs", "/tmp:size=256m,mode=1777",
        "--mount", "type=volume,source=$DatabaseVolume,target=/app/state/database,readonly",
        "--mount", "type=bind,source=$BackupDir,target=/app/state/backups",
        "--entrypoint", "python3",
        $Image,
        "/app/scripts/create_start_stop_backup.py",
        "--database", "/app/state/database/start-stop.sqlite3",
        "--backup-dir", "/app/state/backups"
    )
    if ($ComposeArguments.Count -gt 1) {
        $BackupArguments += $ComposeArguments[1..($ComposeArguments.Count - 1)]
    }
    & $docker.Source @BackupArguments
    if ($LASTEXITCODE -ne 0) {
        throw "启停数据库备份失败，退出码：$LASTEXITCODE"
    }
    exit 0
}

& $docker.Source compose `
    --project-directory $projectDir `
    -f (Join-Path $projectDir "compose.yaml") `
    -f (Join-Path $projectDir "compose.windows.yaml") `
    @ComposeArguments
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose 执行失败，退出码：$LASTEXITCODE"
}
