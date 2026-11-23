<#
  Nightly backup of Claude Code sessions to GCS.

  For each folder in ~/.claude/projects:
    - short name = folder name minus "stripPrefix" from sync-config.json, e.g. "C--Users-<you>-Documents-" (case-insensitive)
    - in "include"  -> gcloud storage rsync to <bucket>/<short name>/ (adds/updates only, never deletes)
    - in "exclude"  -> skipped
    - in neither    -> skipped and reported as "unreviewed" so new projects don't go unnoticed

  Authenticates only as the service account, via an isolated CLOUDSDK_CONFIG.
  Usage:  powershell -NoProfile -ExecutionPolicy Bypass -File sync-sessions.ps1 [-DryRun]
#>
param([switch]$DryRun)

$ErrorActionPreference = 'Continue'
$configPath = Join-Path $PSScriptRoot 'sync-config.json'
$cfg = Get-Content $configPath -Raw | ConvertFrom-Json

New-Item -ItemType Directory -Force $cfg.logDir | Out-Null
$logFile = Join-Path $cfg.logDir ("sync-{0:yyyy-MM-dd}.log" -f (Get-Date))

function Write-Log([string]$msg) {
    $line = "{0:yyyy-MM-dd HH:mm:ss}  {1}" -f (Get-Date), $msg
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Output $line
}

function Show-Toast([string]$title, [string]$body) {
    try {
        [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
        [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null
        $esc = { param($s) [Security.SecurityElement]::Escape($s) }
        $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
        $xml.LoadXml("<toast><visual><binding template='ToastGeneric'><text>$(& $esc $title)</text><text>$(& $esc $body)</text></binding></visual></toast>")
        $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'
        [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show(
            [Windows.UI.Notifications.ToastNotification]::new($xml))
    } catch {
        Write-Log "toast failed: $($_.Exception.Message)"
    }
}

function Get-ShortName([string]$folder) {
    $prefix = $cfg.stripPrefix
    if ($folder -ieq $prefix.TrimEnd('-')) { return '_documents-root' }
    if ($folder.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $folder.Substring($prefix.Length).ToLowerInvariant()
    }
    return $folder.ToLowerInvariant()   # project outside ~/Documents: keep full encoded name
}

$env:CLOUDSDK_CONFIG = $cfg.cloudsdkConfig
$env:CLOUDSDK_CORE_DISABLE_PROMPTS = '1'

Write-Log "=== sync start$(if ($DryRun) { ' (DRY RUN)' }) ==="

$include = @($cfg.include | ForEach-Object { $_.ToLowerInvariant() })
$exclude = @($cfg.exclude | ForEach-Object { $_.ToLowerInvariant() })
$failed = @(); $synced = @(); $unreviewed = @()

foreach ($dir in Get-ChildItem -Path $cfg.projectsDir -Directory) {
    $short = Get-ShortName $dir.Name
    if ($exclude -contains $short) { continue }
    if ($include -notcontains $short) { $unreviewed += $short; continue }

    $dest = "$($cfg.bucket)/$short"
    $gargs = @('storage', 'rsync', $dir.FullName, $dest, '--recursive', '--no-user-output-enabled')
    if ($DryRun) { $gargs += '--dry-run'; $gargs = $gargs | Where-Object { $_ -ne '--no-user-output-enabled' } }

    Write-Log "sync  $($dir.Name)  ->  $dest"
    $out = & $cfg.gcloud @gargs 2>&1 | ForEach-Object { "$_" }
    $code = $LASTEXITCODE
    $out | Where-Object { $_ -and $_.Trim() } | ForEach-Object { Write-Log "      $_" }
    if ($code -ne 0) { Write-Log "FAIL  $short (exit $code)"; $failed += $short }
    else { $synced += $short }
}

if ($unreviewed) { Write-Log "UNREVIEWED (add to include or exclude in sync-config.json): $($unreviewed -join ', ')" }
Write-Log "=== done: $($synced.Count) synced, $($failed.Count) failed, $($unreviewed.Count) unreviewed ==="

# prune old logs
Get-ChildItem $cfg.logDir -Filter '*.log' |
    Where-Object { $_.LastWriteTime -lt (Get-Date).AddDays(-[int]$cfg.logRetentionDays) } |
    Remove-Item -Force -ErrorAction SilentlyContinue

# --- Graphon (searchable copy: transcripts converted to Markdown + memory) ---
if ($cfg.graphon -and $cfg.graphon.enabled -and -not $DryRun) {
    Write-Log "graphon: running graphon_sync.py (details in graphon-*.log)"
    & $cfg.python -I (Join-Path $PSScriptRoot 'graphon_sync.py') 2>&1 | Select-Object -Last 1 | ForEach-Object { Write-Log "graphon: $_" }
    if ($LASTEXITCODE -ne 0) { Write-Log "FAIL  graphon (exit $LASTEXITCODE)"; $failed += 'graphon' }
}

# --- Codex sessions (raw to GCS under codex/, Markdown to Graphon) ---
if ($cfg.codex -and $cfg.codex.enabled -and -not $DryRun) {
    Write-Log "codex: running codex_sync.py (details in codex-*.log)"
    $codexOut = @(& $cfg.python -I (Join-Path $PSScriptRoot 'codex_sync.py') 2>&1 | ForEach-Object { "$_" })
    $codexExit = $LASTEXITCODE
    $codexOut | Select-Object -Last 1 | ForEach-Object { Write-Log "codex: $_" }
    if ($codexExit -ne 0) { Write-Log "FAIL  codex (exit $codexExit)"; $failed += 'codex' }
    $codexOut | Where-Object { $_ -match 'UNREVIEWED codex projects[^:]*:\s*(.+)$' } | ForEach-Object { $unreviewed += "codex: $($Matches[1])" }
}

if ($failed) {
    Show-Toast 'Claude session backup FAILED' "Failed: $($failed -join ', '). See $logFile"
    exit 1
}
if ($unreviewed) {
    Show-Toast 'Claude session backup: new projects' "Not backed up until reviewed: $($unreviewed -join ', ')"
}
exit 0
