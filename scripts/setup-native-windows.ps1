# Setup nativo Windows per Fase 0 — senza Docker/virtualizzazione
# Installa PostgreSQL 16, Redis (porta Windows), Neo4j Community
# Eseguire come Administrator in PowerShell

$ErrorActionPreference = "Stop"

function Log($msg) { Write-Host "[$(Get-Date -f 'HH:mm:ss')] $msg" -ForegroundColor Cyan }
function Ok($msg)  { Write-Host "[OK] $msg" -ForegroundColor Green }
function Err($msg) { Write-Host "[ERR] $msg" -ForegroundColor Red; exit 1 }

# --- Verifica winget ---
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Err "winget non trovato. Aggiorna Windows 11 o installa App Installer dal Microsoft Store."
}

# ============================================================
# 1. PostgreSQL 16
# ============================================================
Log "Installazione PostgreSQL 16..."
$pgInstalled = winget list --id PostgreSQL.PostgreSQL.16 2>$null | Select-String "PostgreSQL"
if (-not $pgInstalled) {
    winget install --id PostgreSQL.PostgreSQL.16 --silent --accept-package-agreements --accept-source-agreements
    Ok "PostgreSQL 16 installato."
} else { Ok "PostgreSQL 16 gia' installato." }

# Aggiungi psql al PATH per la sessione corrente
$pgBin = "C:\Program Files\PostgreSQL\16\bin"
if (Test-Path $pgBin) {
    $env:PATH = "$pgBin;$env:PATH"
}

# ============================================================
# 2. Redis (porta Windows non ufficiale — funziona senza WSL/Hyper-V)
# ============================================================
Log "Installazione Redis per Windows..."
$redisInstalled = winget list --id tporadowski.redis 2>$null | Select-String "redis"
if (-not $redisInstalled) {
    winget install --id tporadowski.redis --silent --accept-package-agreements --accept-source-agreements
    Ok "Redis installato."
} else { Ok "Redis gia' installato." }

# ============================================================
# 3. Neo4j Community 5
# ============================================================
Log "Installazione Neo4j Community 5..."
$neo4jInstalled = winget list --id Neo4j.Neo4j.Community 2>$null | Select-String "Neo4j"
if (-not $neo4jInstalled) {
    winget install --id Neo4j.Neo4j.Community --silent --accept-package-agreements --accept-source-agreements
    Ok "Neo4j installato."
} else { Ok "Neo4j gia' installato." }

Log "Installazione completata. Esegui setup-native-apply.ps1 per applicare lo schema."
