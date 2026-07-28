# Applica schema Postgres e vincoli Neo4j — ambiente nativo Windows (senza Docker)
# Eseguire DOPO setup-native-windows.ps1 e dopo che i servizi sono avviati

$ErrorActionPreference = "Stop"
$ROOT = Split-Path $PSScriptRoot -Parent

function Log($msg) { Write-Host "[$(Get-Date -f 'HH:mm:ss')] $msg" -ForegroundColor Cyan }
function Ok($msg)  { Write-Host "[OK] $msg" -ForegroundColor Green }
function Err($msg) { Write-Host "[ERR] $msg" -ForegroundColor Red; exit 1 }

# Leggi .env
$envFile = Join-Path $ROOT ".env"
if (Test-Path $envFile) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*([^#][^=]+)=(.*)$') {
            [System.Environment]::SetEnvironmentVariable($Matches[1].Trim(), $Matches[2].Trim(), "Process")
        }
    }
}

$pgPassword = $env:POSTGRES_PASSWORD
if (-not $pgPassword) { $pgPassword = "changeme" }

# ============================================================
# 1. Crea utente e database Postgres
# ============================================================
Log "Configurazione PostgreSQL..."
$pgBin = "C:\Program Files\PostgreSQL\16\bin"
$env:PATH = "$pgBin;$env:PATH"
$env:PGPASSWORD = $pgPassword

# Controlla che Postgres sia avviato
$svc = Get-Service -Name "postgresql*" -ErrorAction SilentlyContinue | Select-Object -First 1
if (-not $svc) { Err "Servizio PostgreSQL non trovato. Hai eseguito setup-native-windows.ps1?" }
if ($svc.Status -ne "Running") {
    Log "Avvio servizio PostgreSQL..."
    Start-Service $svc.Name
    Start-Sleep 3
}

# Crea ruolo e database (ignora errori se esistono gia')
$env:PGPASSWORD = "postgres"   # password superuser impostata dall'installer
psql -U postgres -c "CREATE USER finops WITH PASSWORD '$pgPassword';" 2>$null
psql -U postgres -c "CREATE DATABASE finops OWNER finops;" 2>$null
psql -U postgres -c "GRANT ALL PRIVILEGES ON DATABASE finops TO finops;" 2>$null
Ok "Utente e database 'finops' pronti."

# Applica migration
Log "Applicazione schema SQL..."
$env:PGPASSWORD = $pgPassword
psql -U finops -d finops -f (Join-Path $ROOT "migrations\001_init.sql")
Ok "Schema Postgres applicato."

# Verifica tabelle
Log "Verifica tabelle..."
$tables = psql -U finops -d finops -c "\dt" 2>&1
Write-Host $tables
$required = @("jobs","documents","document_chunks","raw_resources","mandatory_resource_types","tenant_tagging_rules","arbitration_requests","tag_proposals","audit_log")
$missing = $required | Where-Object { $tables -notmatch $_ }
if ($missing) { Err "Tabelle mancanti: $($missing -join ', ')" }
Ok "Tutte le 9 tabelle presenti."

# ============================================================
# 2. Verifica Redis
# ============================================================
Log "Verifica Redis..."
$redisCli = "C:\Program Files\Redis\redis-cli.exe"
if (-not (Test-Path $redisCli)) {
    # Cerca in path alternativo
    $redisCli = (Get-Command redis-cli -ErrorAction SilentlyContinue)?.Source
}
if ($redisCli) {
    $pong = & $redisCli ping 2>&1
    if ($pong -eq "PONG") { Ok "Redis risponde (PONG)." }
    else { Err "Redis non risponde: $pong" }
} else {
    Log "redis-cli non trovato nel PATH — verifica manualmente con: redis-cli ping"
}

# ============================================================
# 3. Neo4j — applica vincoli via cypher-shell
# ============================================================
Log "Applicazione vincoli Neo4j..."
$neo4jPassword = $env:NEO4J_PASSWORD
if (-not $neo4jPassword) { $neo4jPassword = "changeme" }

# Cerca cypher-shell
$cypherShell = "C:\Program Files\Neo4j Community\bin\cypher-shell.bat"
if (-not (Test-Path $cypherShell)) {
    $cypherShell = (Get-Command cypher-shell -ErrorAction SilentlyContinue)?.Source
}

if ($cypherShell) {
    $cypher = Join-Path $ROOT "migrations\neo4j-init.cypher"
    & $cypherShell -a "bolt://localhost:7687" -u neo4j -p $neo4jPassword --file $cypher
    Ok "Vincoli Neo4j applicati."

    # Verifica
    $constraints = & $cypherShell -a "bolt://localhost:7687" -u neo4j -p $neo4jPassword "SHOW CONSTRAINTS;" 2>&1
    Write-Host $constraints
    Ok "Verifica constraint Neo4j completata."
} else {
    Write-Host ""
    Write-Host "ATTENZIONE: cypher-shell non trovato nel PATH." -ForegroundColor Yellow
    Write-Host "Apri Neo4j Browser su http://localhost:7474 e incolla il contenuto di:" -ForegroundColor Yellow
    Write-Host "  migrations\neo4j-init.cypher" -ForegroundColor Yellow
    Write-Host ""
}

Write-Host ""
Write-Host "=== Fase 0 completata ===" -ForegroundColor Green
