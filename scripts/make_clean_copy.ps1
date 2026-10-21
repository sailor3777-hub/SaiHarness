# A clean copy of the project for distribution: code only. No database at all (no chats, connections, profiles or memory):
# the app creates an empty one on first start. The original is not changed.
#   powershell -ExecutionPolicy Bypass -File scripts\make_clean_copy.ps1 -Target D:\SaiHarness_dist
# Not copied: .venv (Run.cmd creates it again), the whole data folder (database, images, notes, logs, mcp.json with paths to
# the original), .claude, backups, test sandboxes, Python caches and .bak files.
param([Parameter(Mandatory = $true)][string]$Target)
$root = Split-Path $PSScriptRoot -Parent
$Target = [System.IO.Path]::GetFullPath($Target)
if ($Target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Target must not be inside the project" }
if ((Test-Path $Target) -and (Get-ChildItem $Target -Force | Select-Object -First 1)) { throw "$Target is not empty: choose another folder" }

$skipDirs = ".venv", ".claude", "__pycache__", "data", "data_backup", "test_sandbox", ".pytest_cache"
robocopy $root $Target /E /XD ($skipDirs | ForEach-Object { Join-Path $root $_ }) /XF *.pyc *.bak /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy failed with code $LASTEXITCODE" }

"Done: ${Target}"
"Start: Run.cmd in this folder (it creates .venv, installs the dependencies and an empty database)."
