# FinOps Tagging Platform — avvio completo
# Eseguire da C:\src\finops-tagging-platform\
# Requisiti: PostgreSQL, Redis, Neo4j già running

$root = $PSScriptRoot

# Legge il .env e costruisce il blocco di assegnazioni da iniettare in ogni finestra
function Get-EnvBlock {
    $lines = Get-Content "$root\.env" -ErrorAction SilentlyContinue
    $assigns = @()
    foreach ($line in $lines) {
        if ($line -match '^\s*#' -or $line -match '^\s*$') { continue }
        if ($line -match '^([A-Z_][A-Z0-9_]*)=(.*)$') {
            $k = $matches[1]
            $v = $matches[2].Trim().Trim('"').Trim("'")
            if ($v -ne '') {
                $assigns += "`$env:$k = '$v'"
            }
        }
    }
    return $assigns -join '; '
}

$envBlock = Get-EnvBlock

function Start-Service {
    param($Title, $Dir, $Command)
    $cmd = "$envBlock; cd '$Dir'; $Command"
    Start-Process powershell -ArgumentList "-NoExit", "-Command", $cmd -WindowStyle Normal
    Write-Host "  Started: $Title" -ForegroundColor Green
}

Write-Host "`nFinOps Tagging Platform — avvio servizi" -ForegroundColor Cyan
Write-Host "=========================================`n"

# 1. Orchestrator API
Start-Service "Orchestrator :8000" `
    "$root\orchestrator" `
    ".venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8000 --reload"

Start-Sleep 2

# 2. Orchestrator RQ Worker
Start-Service "Orchestrator Worker" `
    "$root\orchestrator" `
    ".venv\Scripts\rq.exe worker extraction enrichment arbitration graph_build --worker-class app.worker.WindowsWorker --url redis://localhost:6379/0"

# 3. Agent 1 — Resource Extractor
Start-Service "Agent1 Extractor :8001" `
    "$root\agent1_resource_extractor" `
    ".venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8001 --reload"

Start-Service "Agent1 Worker" `
    "$root\agent1_resource_extractor" `
    ".venv\Scripts\rq.exe worker extraction --worker-class app.worker.WindowsWorker --url redis://localhost:6379/0"

# 4. Agent 2 — Tag Enrichment
Start-Service "Agent2 Enrichment :8002" `
    "$root\agent2_tag_enrichment" `
    ".venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8002 --reload"

Start-Service "Agent2 Worker" `
    "$root\agent2_tag_enrichment" `
    ".venv\Scripts\rq.exe worker enrichment --worker-class app.worker.WindowsWorker --url redis://localhost:6379/0"

# 5. Agent 3 — Arbiter
Start-Service "Agent3 Arbiter :8003" `
    "$root\agent3_arbiter" `
    ".venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8003 --reload"

# 6. Agent 4 — Graph Builder
Start-Service "Agent4 Graph :8004" `
    "$root\agent4_graph_builder" `
    ".venv\Scripts\uvicorn.exe app.main:app --host 0.0.0.0 --port 8004 --reload"

Write-Host "`nTutti i servizi avviati. Attendi ~15s per l'inizializzazione." -ForegroundColor Cyan
Write-Host "Health check:"
Start-Sleep 15
@(8000,8001,8002,8003,8004) | ForEach-Object {
    try {
        $r = Invoke-RestMethod "http://localhost:$_/health" -TimeoutSec 4
        Write-Host "  :$_ $($r.service) — OK" -ForegroundColor Green
    } catch {
        Write-Host "  :$_ — NON RISPONDE" -ForegroundColor Red
    }
}
