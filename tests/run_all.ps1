# Запуск тестов SaiHarness.
#   powershell -ExecutionPolicy Bypass -File tests\run_all.ps1            офлайн-набор (без LM Studio и без окна)
#   powershell -ExecutionPolicy Bypass -File tests\run_all.ps1 -Gui       + проверки окна на реальной платформе Qt
#   powershell -ExecutionPolicy Bypass -File tests\run_all.ps1 -Live      + проверки на настоящем LM Studio (localhost:1234)
# Живые тесты обращаются только к уже ЗАГРУЖЕННЫМ моделям и работают на временной базе; не запускай их,
# пока в LM Studio крутится что-то важное. Базу пользователя (data\saiharness.db) тесты не трогают.
param([switch]$Gui, [switch]$Live)
$root = Split-Path $PSScriptRoot -Parent
$python = Join-Path $root ".venv\Scripts\python.exe"
$env:PYTHONPATH = $root
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$offline = "test_richtext", "test_llm_stream", "test_diag", "test_token_estimator", "test_context_guard", "test_connections",
           "test_builder_profiles", "test_mcp_server", "test_tools_router_agent", "test_window_tools", "test_fs_replace",
           "test_fs_replace_window", "test_shell", "test_office_write", "test_themes", "test_bootstrap", "test_max_steps_profile", "test_web_basics", "test_web_fetch_index", "test_web_searxng", "test_web_tools", "test_web_panel", "test_vision", "test_imagegen", "test_images", "test_docs", "test_memory", "test_memory_tools", "test_memory_panel", "test_permission_dialog"
$guiTests = "test_stream_scroll", "test_scroll_to_bottom", "test_render_pixels"
$liveTests = "test_connection_check_watchdog", "test_files_core", "test_files_panel_live", "test_live_chat"

$plan = @(); $offline | ForEach-Object { $plan += @{ Name = $_; Platform = "offscreen" } }
if ($Gui)  { $guiTests | ForEach-Object { $plan += @{ Name = $_; Platform = "" } } }
if ($Live) { $liveTests | ForEach-Object { $plan += @{ Name = $_; Platform = "offscreen" } } }

$failed = 0
foreach ($t in $plan) {
    $db = Join-Path $env:TEMP ("sai_" + $t.Name + ".db")
    if (Test-Path $db) { Remove-Item $db -Force -ErrorAction SilentlyContinue }
    if ($t.Platform) { $env:QT_QPA_PLATFORM = $t.Platform } else { Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue }
    $out = & $python (Join-Path $PSScriptRoot "$($t.Name).py") $db 2>&1 | Out-String
    $fails = ($out -split "`r?`n") | Where-Object { $_ -match "^FAIL" }
    if ($out -match "ALL OK" -and -not $fails) { "{0,-34} ALL OK" -f $t.Name }
    else { $failed++; "{0,-34} НЕ ПРОШЁЛ" -f $t.Name; $fails | ForEach-Object { "    $_" } }
}
Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
if ($failed) { "`nПровалено наборов: $failed"; exit 1 } else { "`nВсе наборы прошли." }
