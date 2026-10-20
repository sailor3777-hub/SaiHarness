# Чистая копия проекта для раздачи: только код. Базы нет совсем (ни чатов, ни подключений, ни профилей, ни памяти):
# приложение создаст пустую при первом запуске. Оригинал не меняется.
#   powershell -ExecutionPolicy Bypass -File scripts\make_clean_copy.ps1 -Target D:\SaiHarness_dist
# Не копируются: .venv (Run.cmd создаст заново), вся папка data (база, картинки, заметки, логи, mcp.json с путями к
# оригиналу), .claude, резервные копии, тестовые песочницы, кеши Python и файлы .bak.
param([Parameter(Mandatory = $true)][string]$Target)
$root = Split-Path $PSScriptRoot -Parent
$Target = [System.IO.Path]::GetFullPath($Target)
if ($Target.StartsWith($root, [System.StringComparison]::OrdinalIgnoreCase)) { throw "Target не должен лежать внутри проекта" }
if ((Test-Path $Target) -and (Get-ChildItem $Target -Force | Select-Object -First 1)) { throw "$Target не пуста: выбери другую папку" }

$skipDirs = ".venv", ".claude", "__pycache__", "data", "data_backup", "test_sandbox", ".pytest_cache"
robocopy $root $Target /E /XD ($skipDirs | ForEach-Object { Join-Path $root $_ }) /XF *.pyc *.bak /NFL /NDL /NJH /NJS /NP | Out-Null
if ($LASTEXITCODE -ge 8) { throw "robocopy завершился с ошибкой $LASTEXITCODE" }

"Готово: ${Target}"
"Запуск: Run.cmd в этой папке (создаст .venv, поставит зависимости и пустую базу)."
