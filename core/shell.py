"""Запуск команды PowerShell для инструмента shell_run (без Qt).

Команда передаётся через -EncodedCommand (никакого экранирования кавычек), вывод stdout+stderr
собирается целиком в UTF-8. По таймауту убивается всё дерево процессов.
"""
import base64
import shutil
import subprocess

TIMEOUT = 60.0
MAX_OUTPUT_CHARS = 1_000_000  # хранимый вывод; больше смысла хранить нет, дальше хвост отбрасывается

# Все потоки сливаются в stdout внутри самого PowerShell: иначе при -EncodedCommand ошибки и прогресс приходят
# в stderr в виде CLIXML (XML вместо текста). Код выхода берём у последней внешней программы.
# UTF8 без BOM: [Text.Encoding]::UTF8 в $OutputEncoding добавляет BOM к тексту, который идёт по конвейеру во внешнюю программу
# (`'Test2' | powershell -File x.ps1` получал «´╗┐Test2»).
_PRELUDE = ("$ProgressPreference = 'SilentlyContinue'; "
            "$u8 = New-Object System.Text.UTF8Encoding($false); [Console]::OutputEncoding = $u8; $OutputEncoding = $u8; ")
_EXIT = "if ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"


def powershell_exe() -> str | None:
    return shutil.which("powershell") or shutil.which("pwsh")


def run(command: str, cwd: str, timeout: float = TIMEOUT) -> tuple[int | None, str, bool]:
    """Возвращает (код выхода, вывод, истёк_ли_таймаут). Код None, если PowerShell не запустился."""
    exe = powershell_exe()
    if exe is None:
        return None, "PowerShell не найден в PATH", False
    script = _PRELUDE + "& {\n" + command + "\n} *>&1 | Out-String -Stream; " + _EXIT
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        proc = subprocess.Popen(
            [exe, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        return None, f"не удалось запустить PowerShell ({e.__class__.__name__})", False
    timed_out = False
    try:
        raw, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        try:
            raw, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            raw = b""
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + "\n[вывод дальше отброшен: больше 1 000 000 символов]"
    return (None if timed_out else proc.returncode), text, timed_out
