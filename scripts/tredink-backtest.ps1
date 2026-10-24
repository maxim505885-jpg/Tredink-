$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $PSScriptRoot
$composeFile = Join-Path $root "docker-compose.tredink.yml"
$baseConfig = Join-Path $root "user_data\tredink.sample.json"
$pairs = @("BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT")
$nowUtc = [DateTime]::UtcNow
$from = $nowUtc.AddDays(-455).ToString("yyyyMMdd")
$to = $nowUtc.AddDays(-2).ToString("yyyyMMdd")
$timerange = "$from-$to"
$identifier = "tredink-eval-$($nowUtc.ToString('yyyyMMddHHmmss'))"
$fee = if ($env:TREDINK_BACKTEST_FEE) { $env:TREDINK_BACKTEST_FEE } else { "0.001" }
$feePct = ([double]$fee * 200) + 0.05
$tempConfigs = [System.Collections.Generic.List[string]]::new()

Push-Location $root
try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "Для walk-forward проверки нужен Docker Desktop с Docker Compose."
    }
    if (-not (Test-Path -LiteralPath ".env")) {
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
    }

    Write-Host "Загружаю 15m/1h/4h историю Binance для $($pairs -join ', ') с $from по $to."
    & docker compose --env-file .env -f $composeFile run --rm --no-deps freqtrade download-data `
        --config /freqtrade/user_data/config_tredink.json `
        --pairs $pairs `
        --timeframes 15m 1h 4h `
        --timerange $timerange
    if ($LASTEXITCODE -ne 0) { throw "Загрузка OHLCV истории завершилась ошибкой." }

    foreach ($pair in $pairs) {
        $safePair = $pair.Replace("/", "-")
        $configName = "tredink-eval-$safePair.json"
        $hostConfig = Join-Path $root "user_data\$configName"
        $containerConfig = "/freqtrade/user_data/$configName"
        $config = Get-Content -LiteralPath $baseConfig -Raw | ConvertFrom-Json
        $config.exchange.pair_whitelist = @($pair)
        $config.max_open_trades = 1
        $config.freqai.identifier = $identifier
        $json = ConvertTo-Json -InputObject $config -Depth 100
        [System.IO.File]::WriteAllText($hostConfig, $json, [System.Text.UTF8Encoding]::new($false))
        $tempConfigs.Add($hostConfig)

        Write-Host "Walk-forward backtest: $pair"
        & docker compose --env-file .env -f $composeFile run --rm --no-deps freqtrade backtesting `
            --config $containerConfig `
            --strategy TredinkForecastStrategy `
            --freqaimodel LightGBMRegressor `
            --timerange $timerange `
            --max-open-trades 1 `
            --dry-run-wallet 1000 `
            --fee $fee `
            --export signals
        if ($LASTEXITCODE -ne 0) { throw "Backtest для $pair завершился ошибкой." }
    }

    Write-Host "Считаю MAE/RMSE, точность направления и фактический ход по сигналам."
    & docker compose --env-file .env -f $composeFile run --rm --no-deps --entrypoint python freqtrade `
        /app/tredink_backtest_report.py `
        --model-dir "/freqtrade/user_data/models/$identifier" `
        --config-path /freqtrade/user_data/tredink.sample.json `
        --pairs $pairs `
        --threshold-pct 0.5 `
        --fee-pct $feePct
    if ($LASTEXITCODE -ne 0) { throw "Не удалось собрать отчёт проверки." }
} finally {
    foreach ($path in $tempConfigs) {
        if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path }
    }
    Pop-Location
}
