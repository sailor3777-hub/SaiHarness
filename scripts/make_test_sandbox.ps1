# Создаёт тестовые папки Alpha и Beta в test_sandbox рядом с проектом.
# Можно запускать повторно: файлы перезаписываются, резервные копии .bak удаляются.
$root = Join-Path (Split-Path $PSScriptRoot -Parent) "test_sandbox"
$alpha = Join-Path $root "Alpha"
$beta = Join-Path $root "Beta"
New-Item -ItemType Directory -Force (Join-Path $alpha "sub"), (Join-Path $alpha ".git"), $beta | Out-Null

$utf8 = New-Object System.Text.UTF8Encoding($false)
$utf8bom = New-Object System.Text.UTF8Encoding($true)
$cp1251 = [System.Text.Encoding]::GetEncoding(1251)
function Write-File($path, $text, $enc) { [System.IO.File]::WriteAllText($path, $text, $enc) }

# --- Alpha ---
Write-File "$alpha\readme.md" "# Alpha`nТестовый проект.`n`n## Установка`nЗапусти ``python main.py```n`nСекретное слово: ФИОЛЕТОВЫЙ-МАМОНТ-42`n" $utf8
Write-File "$alpha\sub\code.py" "def hello(name):`n    return f'Привет, {name}!'`n`n`ndef add(a, b):`n    return a + b`n" $utf8
Write-File "$alpha\cp1251.txt" "Файл в кодировке cp1251.`nВторая строка.`n" $cp1251
Write-File "$alpha\bom.txt" "Файл UTF-8 с BOM.`nВторая строка.`n" $utf8bom
Write-File "$alpha\crlf.txt" "one`r`ntwo`r`nthree`r`n" $utf8
Write-File "$alpha\big.txt" (("строка для большого файла " * 20 + "`n") * 800) $utf8      # больше 300 КБ
[System.IO.File]::WriteAllBytes("$alpha\image.png", [byte[]](0x89,0x50,0x4E,0x47,0x00,0x01,0x02,0x00,0xFF))
Write-File "$alpha\.git\config" "[core]`n    secret = не должно попадать в контекст`n" $utf8

# --- Beta ---
Write-File "$beta\requirements.txt" "pygame-ce>=2.5`r`n`r`nyellow sausage`r`n" $utf8
Write-File "$beta\notes.txt" "Просто заметка.`nТут нет ничего секретного.`n" $utf8
Write-File "$beta\dup.txt" "яблоко и яблоко`n" $utf8

Get-ChildItem $root -Recurse -Filter "*.bak*" -File | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
Write-Host "Готово: $root"
Write-Host "Добавь во вкладке «Файлы» две папки:`n  $alpha`n  $beta"
