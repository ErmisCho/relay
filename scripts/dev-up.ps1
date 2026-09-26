# One-command local dev environment: Postgres, migrations, Delegator, executor.
#
# Usage: .\scripts\dev-up.ps1
# Ctrl-C stops the Delegator and executor and leaves Postgres running
# (docker compose down -v if you want to wipe it too).

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
Set-Location (Split-Path -Parent $PSScriptRoot)

$logDir = Join-Path ([System.IO.Path]::GetTempPath()) ("relay-dev-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $logDir | Out-Null
$delegatorLog = Join-Path $logDir "delegator.log"
$executorLog = Join-Path $logDir "executor.log"
$delegatorProc = $null
$executorProc = $null

function Stop-Dev {
    Write-Host ""
    Write-Host "Stopping delegator/executor..."
    if ($delegatorProc -and -not $delegatorProc.HasExited) { Stop-Process -Id $delegatorProc.Id -Force -ErrorAction SilentlyContinue }
    if ($executorProc -and -not $executorProc.HasExited) { Stop-Process -Id $executorProc.Id -Force -ErrorAction SilentlyContinue }
    Write-Host "Postgres left running (docker compose down -v to wipe it)."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Error "docker not found - install Docker Desktop first."
    exit 1
}

if (-not (Test-Path .env)) {
    Copy-Item .env.example .env
    Write-Host "Created .env from .env.example (edit it to add real API keys if you're not using Ollama)."
}

# Direct calls, not a wrapper function: passing "-d" etc. through a PS function's
# remaining-arguments parameter lets it prefix-match common parameters like -Debug
# and silently swallow the flag. Checking $LASTEXITCODE inline avoids that entirely.

Write-Host "Installing dependencies (uv sync --extra dev)..."
uv sync --extra dev
if ($LASTEXITCODE -ne 0) { throw "uv sync failed (exit $LASTEXITCODE)" }

Write-Host "Starting Postgres..."
docker compose up -d --wait postgres
if ($LASTEXITCODE -ne 0) { throw "docker compose up failed (exit $LASTEXITCODE)" }

Write-Host "Applying migrations..."
uv run alembic upgrade head
if ($LASTEXITCODE -ne 0) { throw "alembic upgrade failed (exit $LASTEXITCODE)" }

if (Get-Command ollama -ErrorAction SilentlyContinue) {
    try {
        Invoke-WebRequest -Uri "http://localhost:11434" -UseBasicParsing -TimeoutSec 2 | Out-Null
    } catch {
        Write-Warning "Ollama is installed but not responding on :11434 - start it, or edit .env to point DELEGATOR_MODEL/etc. at Anthropic/OpenAI instead."
    }
}

$env:RELAY_ALLOW_DEV_SECRET = "1"

Write-Host "Starting Delegator (:8000) and executor (:8001)... logs: $logDir"
$delegatorProc = Start-Process -FilePath "uv" -ArgumentList "run", "python", "-m", "relay.delegator" `
    -RedirectStandardOutput $delegatorLog -RedirectStandardError "$delegatorLog.err" -PassThru -NoNewWindow
$executorProc = Start-Process -FilePath "uv" -ArgumentList "run", "python", "-m", "relay.executor" `
    -RedirectStandardOutput $executorLog -RedirectStandardError "$executorLog.err" -PassThru -NoNewWindow

Start-Sleep -Seconds 1
if ($delegatorProc.HasExited) {
    Write-Error "Delegator failed to start - see $delegatorLog / $delegatorLog.err"
    Get-Content "$delegatorLog.err" -ErrorAction SilentlyContinue
    exit 1
}
if ($executorProc.HasExited) {
    Write-Error "Executor failed to start - see $executorLog / $executorLog.err"
    Get-Content "$executorLog.err" -ErrorAction SilentlyContinue
    exit 1
}

Write-Host ""
Write-Host "Everything is up."
Write-Host "  Delegator: http://127.0.0.1:8000  (log: $delegatorLog)"
Write-Host "  Executor:  http://127.0.0.1:8001  (log: $executorLog)"
Write-Host ""
$secretLine = Select-String -Path .env -Pattern '^DELEGATOR_SHARED_SECRET=(.*)$' | Select-Object -First 1
$secret = if ($secretLine) { $secretLine.Matches[0].Groups[1].Value } else { "dev-secret-change-me" }

Write-Host "Try it (use curl.exe, not the Invoke-WebRequest alias, for real streaming):"
Write-Host "  curl.exe -N http://127.0.0.1:8000/v1/chat/completions ``"
Write-Host "    -H ""Authorization: Bearer $secret"" ``"
Write-Host "    -H ""Content-Type: application/json"" ``"
Write-Host "    -d ""@tests/delegator/fixtures/elevenlabs_custom_llm_request.json"""
Write-Host ""
Write-Host "Ctrl-C to stop."

try {
    Get-Content -Path $delegatorLog, $executorLog -Wait -Tail 20
} finally {
    Stop-Dev
}
