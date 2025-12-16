# commit.ps1 — Безопасный скрипт для коммита и пуша на GitHub
# Расположение: D:\Python project\bot\main\commit.ps1

Write-Host "=== GIT COMMIT & PUSH ===" -ForegroundColor Cyan
Write-Host ""

# Переходим в корень проекта
Set-Location "D:\Python project\bot"

# Показываем статус
Write-Host "📋 Текущий статус:" -ForegroundColor Yellow
git status --short

# Проверка на секреты в изменённых файлах
Write-Host ""
Write-Host "🔍 Проверка на секреты..." -ForegroundColor Yellow
$dangerousPatterns = @("API_KEY", "TOKEN", "SECRET", "PASSWORD", "ADMIN_ID")
$stagedFiles = git diff --cached --name-only
$foundSecrets = $false

foreach ($file in $stagedFiles) {
    if ($file -and (Test-Path $file)) {
        foreach ($pattern in $dangerousPatterns) {
            $matches = Select-String -Path $file -Pattern $pattern -SimpleMatch
            if ($matches) {
                Write-Host "⚠️  ВНИМАНИЕ: Найден '$pattern' в файле $file" -ForegroundColor Red
                $foundSecrets = $true
            }
        }
    }
}

if ($foundSecrets) {
    Write-Host ""
    Write-Host "❌ Обнаружены потенциальные секреты! Проверьте файлы." -ForegroundColor Red
    $continue = Read-Host "Продолжить всё равно? (y/n)"
    if ($continue -ne "y") {
        Write-Host "Отменено." -ForegroundColor Gray
        exit
    }
} else {
    Write-Host "✅ Секреты не найдены" -ForegroundColor Green
}

# Запрашиваем сообщение коммита
Write-Host ""
$commitMsg = Read-Host "📝 Сообщение коммита"

if ([string]::IsNullOrWhiteSpace($commitMsg)) {
    Write-Host "❌ Сообщение не может быть пустым!" -ForegroundColor Red
    exit
}

# Добавляем все изменения
git add -A

# Показываем что будет закоммичено
Write-Host ""
Write-Host "📦 Будет закоммичено:" -ForegroundColor Yellow
git diff --cached --stat

# Подтверждение
Write-Host ""
$confirm = Read-Host "✅ Коммитить и пушить? (y/n)"

if ($confirm -eq "y") {
    Write-Host ""
    Write-Host "🔄 Коммит..." -ForegroundColor Cyan
    git commit -m $commitMsg
    
    Write-Host ""
    Write-Host "🔄 Синхронизация с origin..." -ForegroundColor Cyan
    git pull --rebase origin main
    
    Write-Host ""
    Write-Host "🚀 Push на GitHub..." -ForegroundColor Cyan
    git push origin main
    
    Write-Host ""
    Write-Host "✅ Готово!" -ForegroundColor Green
} else {
    # Откатываем git add
    git reset HEAD
    Write-Host "❌ Отменено." -ForegroundColor Gray
}
