$ErrorActionPreference = "Stop"

$composeFile = Join-Path $PSScriptRoot "docker-compose.tredink.yml"
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker Desktop with Docker Compose is required. Install/start Docker Desktop, then run this script again."
}

Push-Location $PSScriptRoot
try {
    if (-not (Test-Path -LiteralPath ".env")) {
        Copy-Item -LiteralPath ".env.example" -Destination ".env"
        Write-Host "Создан локальный .env. Впишите Telegram token и chat ID в файл, чтобы включить сообщения."
    }

    & docker compose --env-file .env -f $composeFile up -d
    if ($LASTEXITCODE -ne 0) {
        throw "Tredink could not start. Check the Docker Desktop status and run: docker compose -f docker-compose.tredink.yml logs"
    }
    Write-Host "Tredink is starting at http://127.0.0.1:8500"
    Write-Host "First forecast training can take several minutes."
} finally {
    Pop-Location
}
