# SaiHarness agent uninstaller: removes the remote agent from this host.
#
# Undoes what installer.ps1 did: stops the running agent, removes its logon
# task(s) and deletes the install folder (package, venv, certificates, config,
# logs). Python itself is left alone: other programs may use it.
# The installer leaves a copy of this script in the install folder.
#
# Run on the agent host (PowerShell):
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1         (asks first)
#   powershell -ExecutionPolicy Bypass -File uninstall.ps1 -Yes    (no question)
#
# On the main harness, delete the agent on the Agent tab as well (or revoke it):
# its certificate stays valid there until you do.

param(
    [switch]$Yes,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'SaiHarnessAgent')
)

$ErrorActionPreference = 'Stop'
$InstallDir = [IO.Path]::GetFullPath($InstallDir).TrimEnd('\')

# Only what belongs to this install folder: logon tasks whose action points into it
# and agent processes started from it.
function Get-AgentTasks {
    try {
        Get-ScheduledTask -TaskName 'SaiHarnessAgent-*' -ErrorAction Stop | Where-Object {
            @($_.Actions | Where-Object { "$($_.Execute) $($_.Arguments)" -like "*$InstallDir*" }).Count -gt 0
        }
    } catch { @() }
}
function Get-AgentProcesses {
    Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction SilentlyContinue | Where-Object {
        $_.CommandLine -and $_.CommandLine -like '*agent_host.py*' -and $_.CommandLine -like "*$InstallDir*"
    }
}

$Tasks = @(Get-AgentTasks)
$Procs = @(Get-AgentProcesses)
$HasDir = Test-Path -LiteralPath $InstallDir

if (-not $HasDir -and $Tasks.Count -eq 0 -and $Procs.Count -eq 0) {
    Write-Host "No SaiHarness agent found in $InstallDir. Nothing to do."
    exit 0
}

$Name = ''
try { $Name = (Get-Content -LiteralPath (Join-Path $InstallDir 'agent.json') -Raw | ConvertFrom-Json).name } catch {}

Write-Host "SaiHarness agent$(if ($Name) { " '$Name'" }) on this host:"
Write-Host "  folder:     $(if ($HasDir) { $InstallDir } else { '(none)' })"
Write-Host "  logon task: $(if ($Tasks.Count) { ($Tasks | ForEach-Object { $_.TaskName }) -join ', ' } else { '(none)' })"
Write-Host "  running:    $(if ($Procs.Count) { 'pid ' + (($Procs | ForEach-Object { $_.ProcessId }) -join ', ') } else { 'no' })"

if (-not $Yes) {
    $Answer = Read-Host 'Remove it? [y/N]'
    if ($Answer -notmatch '^(y|yes)$') { Write-Host 'Cancelled.'; exit 1 }
}

# 1. The logon task first, so nothing restarts the agent.
foreach ($T in $Tasks) {
    try {
        Stop-ScheduledTask -TaskName $T.TaskName -TaskPath $T.TaskPath -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $T.TaskName -TaskPath $T.TaskPath -Confirm:$false
        Write-Host "Removed logon task $($T.TaskName)."
    } catch {
        Write-Host "Could not remove logon task $($T.TaskName): $($_.Exception.Message)"
    }
}

# 2. The running agent (and anything it started, e.g. a shell command).
# taskkill's stderr would be a terminating error under 'Stop' in PowerShell 5.1, and it does write there when a
# process already went down with its parent (a venv's python.exe is a launcher with a child of the same command).
$ErrorActionPreference = 'Continue'
foreach ($P in @(Get-AgentProcesses)) {
    & taskkill.exe /PID $P.ProcessId /T /F 2>&1 | Out-Null
    Write-Host "Stopped agent process $($P.ProcessId)."
}
$ErrorActionPreference = 'Stop'

# 3. The folder. A just-killed process may hold files for a moment: retry.
if ($HasDir) {
    Set-Location -LiteralPath $env:TEMP   # never delete the folder we stand in
    $Removed = $false
    for ($i = 0; $i -lt 10 -and -not $Removed; $i++) {
        try {
            Remove-Item -LiteralPath $InstallDir -Recurse -Force -ErrorAction Stop
            $Removed = $true
        } catch {
            Start-Sleep -Milliseconds 500
        }
    }
    if ($Removed -or -not (Test-Path -LiteralPath $InstallDir)) {
        Write-Host "Deleted $InstallDir."
    } else {
        Write-Host "Could not delete $InstallDir completely (a file is in use). Close it and run this script again."
        exit 2
    }
}

Write-Host 'The agent is uninstalled. On the main harness, delete it on the Agent tab too.'
exit 0
