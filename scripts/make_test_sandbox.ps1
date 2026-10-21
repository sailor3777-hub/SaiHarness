# Creates the test folders Alpha and Beta in test_sandbox next to the project.
# Can be run again: files are overwritten, .bak backups are deleted.
$root = Join-Path (Split-Path $PSScriptRoot -Parent) "test_sandbox"
$alpha = Join-Path $root "Alpha"
$beta = Join-Path $root "Beta"
New-Item -ItemType Directory -Force (Join-Path $alpha "sub"), (Join-Path $alpha ".git"), $beta | Out-Null

$utf8 = New-Object System.Text.UTF8Encoding($false)
$utf8bom = New-Object System.Text.UTF8Encoding($true)
$cp1251 = [System.Text.Encoding]::GetEncoding(1251)
function Write-File($path, $text, $enc) { [System.IO.File]::WriteAllText($path, $text, $enc) }

# --- Alpha ---
Write-File "$alpha\readme.md" "# Alpha`nTest project.`n`n## Installation`nRun ``python main.py```n`nSecret word: PURPLE-MAMMOTH-42`n" $utf8
Write-File "$alpha\sub\code.py" "def hello(name):`n    return f'Hello, {name}!'`n`n`ndef add(a, b):`n    return a + b`n" $utf8
Write-File "$alpha\cp1251.txt" "Файл в кодировке cp1251.`nВторая строка.`n" $cp1251      # Russian on purpose: checks cp1251 decoding
Write-File "$alpha\bom.txt" "UTF-8 file with a BOM.`nSecond line.`n" $utf8bom
Write-File "$alpha\crlf.txt" "one`r`ntwo`r`nthree`r`n" $utf8
Write-File "$alpha\big.txt" (("a line for a big file " * 20 + "`n") * 800) $utf8      # over 300 KB
[System.IO.File]::WriteAllBytes("$alpha\image.png", [byte[]](0x89,0x50,0x4E,0x47,0x00,0x01,0x02,0x00,0xFF))
Write-File "$alpha\.git\config" "[core]`n    secret = must not get into the context`n" $utf8

# --- Beta ---
Write-File "$beta\requirements.txt" "pygame-ce>=2.5`r`n`r`nyellow sausage`r`n" $utf8
Write-File "$beta\notes.txt" "Just a note.`nThere is nothing secret here.`n" $utf8
Write-File "$beta\dup.txt" "apple and apple`n" $utf8

Get-ChildItem $root -Recurse -Filter "*.bak*" -File | ForEach-Object { Remove-Item -LiteralPath $_.FullName -Force }
Write-Host "Done: $root"
Write-Host "Add two folders on the Files tab:`n  $alpha`n  $beta"
