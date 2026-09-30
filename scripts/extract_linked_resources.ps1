<#
Extract linked-resource JSON files for any AWS account, across regions
(equivalente PowerShell di extract_linked_resources.py: stessi file, stessi filtri,
stesso formato di stato/ripresa).

Uso:
  .\scripts\extract_linked_resources.ps1 -AwsProfile <profilo> [-Account <id>] [-Regions all] [-OutRoot extracted] [-Fresh]

  -Account : default = account del profilo (sts get-caller-identity)
  -Regions : "all" (default) = tutte le regioni abilitate, oppure "eu-south-1,eu-west-1"
  -Fresh   : ignora lo stato salvato ed estrae tutto da capo
  -RetryDenied : ritenta anche sezioni/regioni negate da SCP/IAM (es. dopo un cambio permessi)
Output: <OutRoot>\<account>\<regione>\*.json, mantenuto solo per le regioni con risorse:
  acm_inuseby.json, acm_inuseby_full.json, cloudformation_stacks.json, config_rules.json,
  eip_associations.json, eni_attachments.json, instances_for_volumes.json,
  snapshots_volumeid.json, ssm_managedinstances.json, volumes_all.json,
  volumes_live.json, volumes_report.json (+ instances_all.json).

Robustezza / ripresa:
- ogni sezione viene scritta su disco appena completata (scrittura atomica), con lo
  stato in <regione>\_state.json; l'avanzamento dell'account e' in <account>\_run.json.
  Rilanciando lo stesso comando dopo un'interruzione, le sezioni completate vengono
  rilette da disco e AWS viene chiamato solo per il resto.
- nei cicli per-elemento (certificati ACM, stack, regole Config, tag SSM) ogni elemento
  viene accodato a <regione>\_<sezione>.partial.jsonl appena ottenuto.
- una chiamata AWS fallita coinvolge solo la propria sezione ({"error": "..."}), che
  viene ritentata al run successivo. I dinieghi permanenti (SCP "explicit deny",
  UnauthorizedOperation, AccessDenied, ...) sono salvati come {"error": "...", "denied": true}
  e NON vengono ritentati (stato regione "denied" / "done_with_denied"), salvo -RetryDenied.
- la percentuale e' pesata sul lavoro reale: ogni sezione e ogni elemento dei cicli lunghi
  vale un'unita'.
Avanzamento: una riga per sezione completata con la percentuale complessiva,
contatori per-elemento nei cicli lunghi e barra Write-Progress.
#>
param(
    [Parameter(Mandatory = $true)][string]$AwsProfile,
    [string]$Account,
    [string]$Regions = "all",
    [string]$OutRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) "extracted"),
    [switch]$Fresh,
    [switch]$RetryDenied
)

$ErrorActionPreference = "Stop"
$HomeRegion = "eu-south-1"
$ReportScript = Join-Path $PSScriptRoot "build_volumes_report.py"
$Utf8 = New-Object System.Text.UTF8Encoding $false
$env:AWS_RETRY_MODE = "adaptive"
$env:AWS_MAX_ATTEMPTS = "10"

function Get-Now { (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss+00:00") }

function Invoke-AwsJson([string]$Region, [string[]]$AwsArgs) {
    $out = & aws @AwsArgs --profile $AwsProfile --region $Region --output json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "aws $($AwsArgs -join ' ') [$Region] failed: $out" }
    $text = ($out | Out-String).Trim()
    if ($text) { return $text | ConvertFrom-Json }
}

function New-ErrorObject([string]$Message) { [pscustomobject]@{ error = $Message } }

# Dinieghi permanenti (SCP dell'organizzazione, IAM): ritentarli non serve.
# Throttling, timeout e simili restano "error" e vengono ritentati.
$DeniedPatterns = @("explicit deny", "UnauthorizedOperation", "AccessDenied", "not authorized to perform", "AuthFailure")
function Test-DeniedMessage([string]$Message) { foreach ($p in $DeniedPatterns) { if ($Message.Contains($p)) { return $true } }; return $false }
function New-DeniedObject([string]$Message) { [pscustomobject]@{ error = $Message; denied = $true } }
function Test-Denied($Data) { return (Test-Failed $Data) -and ($Data.PSObject.Properties.Name -contains "denied") -and $Data.denied }

function Test-Failed($Data) { return ($Data -isnot [array]) -and ($null -ne $Data) -and ($Data.PSObject.Properties.Name -contains "error") }

# Righe di avanzamento direttamente su stdout (visibili anche se l'output e' rediretto su file);
# Write-Output dentro le funzioni finirebbe invece nei valori di ritorno.
function Write-Log([string]$Message) {
    [Console]::Out.WriteLine($Message)
    [Console]::Out.Flush()
}

# Scrittura atomica, UTF-8 senza BOM (Set-Content -Encoding UTF8 di PowerShell 5.1 aggiunge il BOM)
function Save-Json([string]$Path, $Data) {
    if (Test-Failed $Data) { $json = ConvertTo-Json -InputObject $Data -Depth 100 }
    elseif ($Data -is [System.Collections.IDictionary]) { $json = ConvertTo-Json -InputObject ([pscustomobject]$Data) -Depth 100 }
    elseif (@($Data).Count -eq 0) { $json = "[]" }
    else { $json = ConvertTo-Json -InputObject @($Data) -Depth 100 }
    $tmp = "$Path.tmp"
    [System.IO.File]::WriteAllText($tmp, $json, $Utf8)
    Move-Item -LiteralPath $tmp -Destination $Path -Force
}

# Gli array vengono restituiti intatti (anche vuoti o con un solo elemento), senza srotolarli
function Read-Json([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try {
        $text = [System.IO.File]::ReadAllText($Path)
        $obj = $text | ConvertFrom-Json
    } catch { return $null }
    if ($text.TrimStart().StartsWith("[")) { return , @($obj | ForEach-Object { $_ }) }
    return $obj
}

# ---------------------------------------------------------------------------
# Avanzamento (console + Write-Progress + <account>\_run.json)
# ---------------------------------------------------------------------------
# Percentuale pesata sul lavoro reale: ogni sezione vale 1 unita' e ogni elemento di un ciclo
# lungo (certificato ACM, stack, regola, istanza SSM) vale 1 unita'. Quando un ciclo scopre
# N elementi il totale cresce di N, quindi un ciclo da 920 certificati sposta davvero la percentuale.
$script:Done = 0
$script:Total = 0
$script:ItemsDone = 0
$script:ItemsTotal = 0
$script:Run = [ordered]@{}

function Get-Pct {
    $total = $script:Total + $script:ItemsTotal
    if ($total) { return 100.0 * ($script:Done + $script:ItemsDone) / $total } else { return 100.0 }
}

function Format-Pct { [string]::Format([System.Globalization.CultureInfo]::InvariantCulture, "[{0,5:F1}%]", (Get-Pct)) }

function Save-Run {
    $script:Run["progress_pct"] = [math]::Round((Get-Pct), 1)
    $script:Run["sections_done"] = $script:Done
    $script:Run["sections_total"] = $script:Total
    $script:Run["items_done"] = $script:ItemsDone
    $script:Run["items_total"] = $script:ItemsTotal
    $script:Run["updated_at"] = Get-Now
    Save-Json $script:RunPath $script:Run
}

function Write-Section([string]$Region, [string]$Name, [string]$Status, [string]$Detail) {
    $script:Done++
    Write-Log ("{0} ({1}/{2}) {3,-15} {4,-16} {5}  {6}" -f (Format-Pct), $script:Done, $script:Total, $Region, $Name, $Status, $Detail)
    Write-Progress -Id 1 -Activity "Estrazione account $Account" -Status "$Region - $Name ($($script:Done)/$($script:Total))" -PercentComplete ([math]::Min(100, [int](Get-Pct)))
    Save-Run
}

function Set-RegionStatus([string]$Region, [string]$Status) {
    $script:Run["regions"][$Region] = $Status
    Save-Run
}

function Format-Count($Data) { if (Test-Denied $Data) { "denied" } elseif (Test-Failed $Data) { "error" } else { "$(@($Data).Count) items" } }

# Applica $Fn a ogni elemento, accodando subito ogni risultato a _<sezione>.partial.jsonl:
# alla ripresa gli elementi gia' elaborati non vengono richiesti di nuovo.
function Invoke-Items([string]$Region, [string]$OutDir, [string]$Section, $Items, [scriptblock]$Key, [scriptblock]$Fn) {
    $Items = @($Items | Where-Object { $null -ne $_ })
    $partial = Join-Path $OutDir "_$Section.partial.jsonl"
    $results = @{}
    if (Test-Path -LiteralPath $partial) {
        foreach ($line in [System.IO.File]::ReadAllLines($partial)) {
            try { $rec = $line | ConvertFrom-Json; $results[$rec.key] = $rec.value } catch { }  # riga troncata da un crash
        }
        if ($results.Count) { Write-Log ("{0,19} {1,-15} {2,-16} resume: {3}/{4} from disk" -f "", $Region, $Section, $results.Count, $Items.Count) }
    }
    $n = $Items.Count
    $script:ItemsTotal += $n
    $script:ItemsDone += @($Items | Where-Object { $results.ContainsKey((& $Key $_)) }).Count
    foreach ($item in $Items) {
        $k = & $Key $item
        if ($results.ContainsKey($k)) { continue }
        $value = & $Fn $item
        $line = ConvertTo-Json -InputObject ([pscustomobject]@{ key = $k; value = $value }) -Depth 100 -Compress
        [System.IO.File]::AppendAllText($partial, $line + "`n", $Utf8)
        $results[$k] = $value
        $i = $results.Count
        $script:ItemsDone++
        Write-Progress -Id 2 -ParentId 1 -Activity "$Region $Section" -Status "$i/$n" -PercentComplete ([int](100 * $i / [math]::Max(1, $n)))
        if ($i -eq 1 -or $i -eq $n -or $i % 10 -eq 0) {
            Write-Log ("{0} {1,9} {2,-15} {3,-16} {4}/{5}" -f (Format-Pct), "", $Region, $Section, $i, $n)
            Save-Run
        }
    }
    Write-Progress -Id 2 -ParentId 1 -Activity "$Region $Section" -Completed
    return , @($Items | ForEach-Object { $results[(& $Key $_)] } | Where-Object { $null -ne $_ })
}

# ---------------------------------------------------------------------------
# Sezioni: ciascuna ritorna @{ <file> = <dati> }
# ---------------------------------------------------------------------------
function Get-SsmSection($Region, $OutDir, $Data) {
    $list = (Invoke-AwsJson $Region @("ssm", "describe-instance-information")).InstanceInformationList
    $items = Invoke-Items $Region $OutDir "ssm" $list { param($i) $i.InstanceId } {
        param($inst)
        $tags = $null
        if ($inst.InstanceId -like "mi-*") {
            try { $tags = (Invoke-AwsJson $Region @("ssm", "list-tags-for-resource", "--resource-type", "ManagedInstance", "--resource-id", $inst.InstanceId)).TagList } catch { }
        }
        $inst | Add-Member -NotePropertyName Tags -NotePropertyValue $tags -Force -PassThru
    }
    @{ "ssm_managedinstances.json" = $items }
}

function Get-CloudFormationSection($Region, $OutDir, $Data) {
    $stacks = (Invoke-AwsJson $Region @("cloudformation", "describe-stacks")).Stacks
    $items = Invoke-Items $Region $OutDir "cloudformation" $stacks { param($s) $s.StackId } {
        param($st)
        try { $res = @((Invoke-AwsJson $Region @("cloudformation", "list-stack-resources", "--stack-name", $st.StackId)).StackResourceSummaries) }
        catch { $res = New-ErrorObject "$($_.Exception.Message)" }
        $st | Add-Member -NotePropertyName StackResources -NotePropertyValue $res -Force -PassThru
    }
    @{ "cloudformation_stacks.json" = $items }
}

function Get-EipSection($Region, $OutDir, $Data) {
    @{ "eip_associations.json" = @((Invoke-AwsJson $Region @("ec2", "describe-addresses")).Addresses | Where-Object { $null -ne $_ }) }
}

function Get-AcmSection($Region, $OutDir, $Data) {
    $keyTypes = "keyTypes=RSA_1024,RSA_2048,RSA_3072,RSA_4096,EC_prime256v1,EC_secp384r1,EC_secp521r1"
    $summary = (Invoke-AwsJson $Region @("acm", "list-certificates", "--includes", $keyTypes)).CertificateSummaryList
    $full = Invoke-Items $Region $OutDir "acm" $summary { param($c) $c.CertificateArn } {
        param($cert)
        $detail = (Invoke-AwsJson $Region @("acm", "describe-certificate", "--certificate-arn", $cert.CertificateArn)).Certificate
        if (-not $detail) { return $null }
        $tags = $null
        try { $tags = (Invoke-AwsJson $Region @("acm", "list-tags-for-certificate", "--certificate-arn", $cert.CertificateArn)).Tags } catch { }
        $detail | Add-Member -NotePropertyName Tags -NotePropertyValue $tags -Force -PassThru
    }
    @{
        "acm_inuseby_full.json" = $full
        "acm_inuseby.json"      = @($full | Where-Object { $_.InUseBy -and @($_.InUseBy).Count -gt 0 })
    }
}

function Get-ConfigRulesSection($Region, $OutDir, $Data) {
    $rules = (Invoke-AwsJson $Region @("configservice", "describe-config-rules")).ConfigRules
    $compliance = @{}
    try {
        foreach ($c in (Invoke-AwsJson $Region @("configservice", "describe-compliance-by-config-rule")).ComplianceByConfigRules) {
            $compliance[$c.ConfigRuleName] = $c.Compliance
        }
    } catch { }
    $items = Invoke-Items $Region $OutDir "config_rules" $rules { param($r) $r.ConfigRuleName } {
        param($r)
        $tags = $null
        try { $tags = (Invoke-AwsJson $Region @("configservice", "list-tags-for-resource", "--resource-arn", $r.ConfigRuleArn)).Tags } catch { }
        $r | Add-Member -NotePropertyName Compliance -NotePropertyValue $compliance[$r.ConfigRuleName] -Force
        $r | Add-Member -NotePropertyName Tags -NotePropertyValue $tags -Force -PassThru
    }
    @{ "config_rules.json" = $items }
}

function Get-EniSection($Region, $OutDir, $Data) {
    @{ "eni_attachments.json" = @((Invoke-AwsJson $Region @("ec2", "describe-network-interfaces")).NetworkInterfaces | Where-Object { $null -ne $_ }) }
}

function Get-VolumesSection($Region, $OutDir, $Data) {
    $volumes = @((Invoke-AwsJson $Region @("ec2", "describe-volumes")).Volumes | Where-Object { $null -ne $_ })
    @{
        "volumes_all.json"  = $volumes
        "volumes_live.json" = @($volumes | Where-Object { $_.State -eq "in-use" })
    }
}

function Get-SnapshotsSection($Region, $OutDir, $Data) {
    @{ "snapshots_volumeid.json" = @((Invoke-AwsJson $Region @("ec2", "describe-snapshots", "--owner-ids", "self")).Snapshots | Where-Object { $_.VolumeId }) }
}

function Get-InstancesSection($Region, $OutDir, $Data) {
    $instances = @(foreach ($res in (Invoke-AwsJson $Region @("ec2", "describe-instances")).Reservations) {
        foreach ($i in $res.Instances) {
            # Oggetto completo + chiavi flat usate da build_volumes_report.py
            $name = ($i.Tags | Where-Object { $_.Key -eq "Name" } | Select-Object -First 1).Value
            $stateDetail = $i.State
            $i | Add-Member -NotePropertyName Name -NotePropertyValue $name -Force
            $i | Add-Member -NotePropertyName StateDetail -NotePropertyValue $stateDetail -Force
            $i | Add-Member -NotePropertyName State -NotePropertyValue $stateDetail.Name -Force -PassThru
        }
    })
    $attached = @{}
    $volumes = $Data["volumes_all.json"]
    if (-not (Test-Failed $volumes)) { foreach ($v in $volumes) { foreach ($a in $v.Attachments) { $attached[$a.InstanceId] = $true } } }
    @{
        "instances_all.json"         = $instances
        "instances_for_volumes.json" = @($instances | Where-Object { $attached.ContainsKey($_.InstanceId) })
    }
}

$ReportInputs = @("volumes_all.json", "instances_for_volumes.json", "snapshots_volumeid.json")

function Get-VolumesReportSection($Region, $OutDir, $Data) {
    foreach ($name in $ReportInputs) {
        if (Test-Failed $Data[$name]) { throw "volumes_report non generato: $name contiene un errore" }
    }
    & python $ReportScript $OutDir | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "build_volumes_report failed" }
    @{ "volumes_report.json" = (Read-Json (Join-Path $OutDir "volumes_report.json")) }
}

$Sections = @(
    @{ Name = "ssm";            Fn = "Get-SsmSection";            Files = @("ssm_managedinstances.json") },
    @{ Name = "cloudformation"; Fn = "Get-CloudFormationSection"; Files = @("cloudformation_stacks.json") },
    @{ Name = "eip";            Fn = "Get-EipSection";            Files = @("eip_associations.json") },
    @{ Name = "acm";            Fn = "Get-AcmSection";            Files = @("acm_inuseby_full.json", "acm_inuseby.json") },
    @{ Name = "config_rules";   Fn = "Get-ConfigRulesSection";    Files = @("config_rules.json") },
    @{ Name = "eni";            Fn = "Get-EniSection";            Files = @("eni_attachments.json") },
    @{ Name = "volumes";        Fn = "Get-VolumesSection";        Files = @("volumes_all.json", "volumes_live.json") },
    @{ Name = "snapshots";      Fn = "Get-SnapshotsSection";      Files = @("snapshots_volumeid.json") },
    @{ Name = "instances";      Fn = "Get-InstancesSection";      Files = @("instances_all.json", "instances_for_volumes.json") },
    @{ Name = "volumes_report"; Fn = "Get-VolumesReportSection";  Files = @("volumes_report.json") }
)

function Invoke-Region([string]$Region) {
    $outDir = Join-Path $script:AccountDir $Region
    New-Item -ItemType Directory -Force $outDir | Out-Null
    $statePath = Join-Path $outDir "_state.json"
    $prev = if ($Fresh) { $null } else { Read-Json $statePath }
    $state = [ordered]@{ status = "running"; started_at = $(if ($prev -and $prev.started_at) { $prev.started_at } else { Get-Now }); sections = [ordered]@{} }
    if ($prev -and $prev.sections) { foreach ($p in $prev.sections.PSObject.Properties) { $state.sections[$p.Name] = $p.Value } }
    Save-Json $statePath $state
    Set-RegionStatus $Region "running"

    $data = @{}
    foreach ($section in $Sections) {
        $paths = @($section.Files | ForEach-Object { Join-Path $outDir $_ })
        $allExist = @($paths | Where-Object { -not (Test-Path -LiteralPath $_) }).Count -eq 0
        $keep = if ($RetryDenied) { @("done") } else { @("done", "denied") }
        if (($keep -contains $state.sections[$section.Name]) -and $allExist) {
            foreach ($f in $section.Files) { $data[$f] = Read-Json (Join-Path $outDir $f) }
            $label = if ($state.sections[$section.Name] -eq "done") { "resumed from disk" } else { "denied (skipped)" }
            Write-Section $Region $section.Name $label (Format-Count $data[$section.Files[0]])
            continue
        }
        try {
            $result = & $section.Fn $Region $outDir $data
            $status = "done"
        } catch {
            $message = "$($_.Exception.Message)"
            # Il report deriva da altre sezioni: e' "denied" se lo e' un suo input
            $derivedDenied = ($section.Name -eq "volumes_report") -and
                (@($ReportInputs | Where-Object { Test-Denied $data[$_] }).Count -gt 0)
            $denied = (Test-DeniedMessage $message) -or $derivedDenied
            $err = if ($denied) { New-DeniedObject $message } else { New-ErrorObject $message }
            $result = @{}
            foreach ($f in $section.Files) { $result[$f] = $err }
            $status = if ($denied) { "denied" } else { "error" }
        }
        foreach ($f in $section.Files) {
            $data[$f] = $result[$f]
            Save-Json (Join-Path $outDir $f) $result[$f]
        }
        $state.sections[$section.Name] = $status
        Save-Json $statePath $state
        $partial = Join-Path $outDir "_$($section.Name).partial.jsonl"
        if ($status -eq "done" -and (Test-Path -LiteralPath $partial)) { Remove-Item -LiteralPath $partial -Force }
        Write-Section $Region $section.Name $status (Format-Count $result[$section.Files[0]])
    }

    # Controlli sulle chiavi: nella pipeline gli array vuoti verrebbero srotolati e persi
    $rawKeys = @($data.Keys | Where-Object { $_ -ne "volumes_report.json" })
    $nonEmpty = @($data.Keys | Where-Object { -not (Test-Failed $data[$_]) -and @($data[$_]).Count -gt 0 })
    if ($nonEmpty.Count -eq 0) {
        Remove-Item -LiteralPath $outDir -Recurse -Force
        $failedKeys = @($rawKeys | Where-Object { (Test-Failed $data[$_]) -and -not (Test-Denied $data[$_]) })
        $okKeys = @($rawKeys | Where-Object { -not (Test-Failed $data[$_]) })
        if ($okKeys.Count -eq 0 -and $failedKeys.Count -gt 0) {
            Set-RegionStatus $Region "failed"
            return "[$Region] ERROR: $($data[$failedKeys[0]].error)"
        }
        # Nessun dato: regione vuota, oppure negata (SCP/IAM) su cio' che contiene risorse
        $deniedKeys = @($rawKeys | Where-Object { Test-Denied $data[$_] })
        if ($deniedKeys.Count -gt 0) {
            Set-RegionStatus $Region "denied"
            return "[$Region] denied by SCP/IAM, no data, skipped"
        }
        Set-RegionStatus $Region "empty"
        return "[$Region] no resources, skipped"
    }
    $pending = @($state.sections.Values | Where-Object { $_ -ne "done" })
    $state.status = if ($pending.Count -eq 0) { "done" }
        elseif (@($pending | Where-Object { $_ -ne "denied" }).Count -eq 0) { "done_with_denied" }
        else { "partial" }
    $state["finished_at"] = Get-Now
    Save-Json $statePath $state
    Set-RegionStatus $Region $state.status
    return "[$Region] written to $outDir ($($state.status))"
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if (-not $Account) { $Account = (Invoke-AwsJson $HomeRegion @("sts", "get-caller-identity")).Account }
if ($Regions -eq "all") {
    $RegionList = @((Invoke-AwsJson $HomeRegion @("ec2", "describe-regions")).Regions.RegionName | Sort-Object)
} else {
    $RegionList = @($Regions -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}

$script:AccountDir = Join-Path $OutRoot $Account
New-Item -ItemType Directory -Force $script:AccountDir | Out-Null
$script:RunPath = Join-Path $script:AccountDir "_run.json"
if ($Fresh -and (Test-Path -LiteralPath $script:RunPath)) { Remove-Item -LiteralPath $script:RunPath -Force }
$prevRun = Read-Json $script:RunPath

# Regioni gia' concluse in un run precedente: non vengono rieseguite
$final = if ($RetryDenied) { @("done", "empty") } else { @("done", "empty", "denied", "done_with_denied") }
$finished = @($RegionList | Where-Object { $prevRun -and $prevRun.regions -and $final -contains $prevRun.regions.$_ })
$script:Total = $RegionList.Count * $Sections.Count
$script:Done = $finished.Count * $Sections.Count
$script:Run["started_at"] = if ($prevRun -and $prevRun.started_at) { $prevRun.started_at } else { Get-Now }
$script:Run["regions"] = [ordered]@{}
foreach ($r in $RegionList) { $script:Run["regions"][$r] = if ($finished -contains $r) { $prevRun.regions.$r } else { "pending" } }
Save-Run

Write-Output "Account ${Account}: $($RegionList.Count) regions, $($Sections.Count) sections each -> $($RegionList -join ', ')"
if ($finished.Count) { Write-Output "Resume: $($finished.Count) regions already completed ($($finished -join ', '))" }
Write-Output "Progress file: $script:RunPath"

$summary = @()
foreach ($Region in $RegionList) {
    if ($finished -contains $Region) { $summary += "[$Region] already completed in a previous run ($($prevRun.regions.$Region))"; continue }
    $summary += Invoke-Region $Region
}
Write-Progress -Id 1 -Activity "Estrazione account $Account" -Completed
$script:Run["finished_at"] = Get-Now
Save-Run

Write-Output ""
$summary | ForEach-Object { Write-Output $_ }
