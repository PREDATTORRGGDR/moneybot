# Деплой бота на сервер: туда уезжает ровно то, что запушено в main на GitHub.
# Незакоммиченные и незапушенные правки не уедут: скрипт остановится и скажет, что сделать.
# Файл сохранён в UTF-8 с BOM: без него Windows PowerShell 5.1 ломает русские строки.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (git status --porcelain) {
    Write-Host "Есть незакоммиченные правки. Сначала: git add -A; git commit -m '...'; git push" -ForegroundColor Red
    exit 1
}
git fetch -q origin
if ((git rev-parse HEAD) -ne (git rev-parse origin/main)) {
    Write-Host "Локальный main не совпадает с GitHub. Сделай git push (или git pull, если правили не тут)" -ForegroundColor Red
    exit 1
}

ssh looksmax "cd /root/moneybot && git pull --ff-only -q && sh server-update.sh"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Деплой не прошёл, смотри ошибку выше" -ForegroundColor Red
    exit 1
}
Write-Host "Готово" -ForegroundColor Green
