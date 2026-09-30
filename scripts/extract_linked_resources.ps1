<#
Extract linked-resource JSON files for any AWS account, across regions
(equivalente PowerShell di extract_linked_resources.py: stessi file, stessi filtri).

Uso:
  .\scripts\extract_linked_resources.ps1 -AwsProfile <profilo> [-Account <id>] [-Regions all] [-OutRoot extracted]

  -Account : default = account del profilo (sts get-caller-identity)
  -Regions : "all" (default) = tutte le regioni abilitate, oppure "eu-south-1,eu-west-1"
Output: <OutRoot>\<account>\<regione>\*.json solo per le regioni con risorse:
  acm_inuseby.json, acm_inuseby_full.json, cloudformation_stacks.json, config_rules.json,
  eip_associations.json, eni_attachments.json, instances_for_volumes.json,
  snapshots_volumeid.json, ssm_managedinstances.json, volumes_all.json,
  volumes_live.json, volumes_report.json (+ instances_all.json).
Una chiamata AWS fallita coinvolge solo il proprio file, che contiene {"error": "..."}.
#>
param(
    [Parameter(Mandatory = $true)][string]$AwsProfile,
    [string]$Account,
    [string]$Regions = "all",
    [string]$OutRoot = (Join-Path (Split-Path $PSScriptRoot -Parent) "extracted")
)

$ErrorActionPreference = "Stop"
$HomeRegion = "eu-south-1"
$ReportScript = Join-Path $PSScriptRoot "build_volumes_report.py"
$env:AWS_RETRY_MODE = "adaptive"
$env:AWS_MAX_ATTEMPTS = "10"

function Invoke-AwsJson([string]$Region, [string[]]$AwsArgs) {
    $out = & aws @AwsArgs --profile $AwsProfile --region $Region --output json 2>&1
    if ($LASTEXITCODE -ne 0) { throw "aws $($AwsArgs -join ' ') [$Region] failed: $out" }
    $text = ($out | Out-String).Trim()
    if ($text) { return $text | ConvertFrom-Json }
}

# Esegue un blocco; in caso di errore ritorna @{ error = ... } invece di interrompere la regione
function Get-Safe([scriptblock]$Block) {
    try { return , @(& $Block | Where-Object { $null -ne $_ }) }
    catch { return [pscustomobject]@{ error = "$($_.Exception.Message)" } }
}

function Test-Failed($Data) { return ($Data -isnot [array]) -and ($null -ne $Data) -and ($Data.PSObject.Properties.Name -contains "error") }

function Save-Json([string]$Path, $Data) {
    if (Test-Failed $Data) { $json = ConvertTo-Json -InputObject $Data -Depth 100 }
    elseif (@($Data).Count -eq 0) { $json = "[]" }
    else { $json = ConvertTo-Json -InputObject @($Data) -Depth 100 }
    # UTF-8 senza BOM (Set-Content -Encoding UTF8 di Windows PowerShell 5.1 aggiunge il BOM)
    [System.IO.File]::WriteAllText($Path, $json, (New-Object System.Text.UTF8Encoding $false))
}

function Get-RegionEvidence([string]$Region) {
    $files = [ordered]@{}

    # SSM managed instances -> linked EC2 instance / hybrid node
    $files["ssm_managedinstances.json"] = Get-Safe {
        foreach ($inst in (Invoke-AwsJson $Region @("ssm", "describe-instance-information")).InstanceInformationList) {
            $tags = $null
            if ($inst.InstanceId -like "mi-*") {
                try { $tags = (Invoke-AwsJson $Region @("ssm", "list-tags-for-resource", "--resource-type", "ManagedInstance", "--resource-id", $inst.InstanceId)).TagList } catch { }
            }
            $inst | Add-Member -NotePropertyName Tags -NotePropertyValue $tags -Force -PassThru
        }
    }

    # CloudFormation stacks + resources they own
    $files["cloudformation_stacks.json"] = Get-Safe {
        foreach ($st in (Invoke-AwsJson $Region @("cloudformation", "describe-stacks")).Stacks) {
            try { $res = @((Invoke-AwsJson $Region @("cloudformation", "list-stack-resources", "--stack-name", $st.StackId)).StackResourceSummaries) }
            catch { $res = [pscustomobject]@{ error = "$($_.Exception.Message)" } }
            $st | Add-Member -NotePropertyName StackResources -NotePropertyValue $res -Force -PassThru
        }
    }

    # EIP associations (all EIPs, incl. unassociated, with link fields)
    $files["eip_associations.json"] = Get-Safe { (Invoke-AwsJson $Region @("ec2", "describe-addresses")).Addresses }

    # ACM certificates (full detail + tags), including InUseBy
    $acm = Get-Safe {
        $keyTypes = "keyTypes=RSA_1024,RSA_2048,RSA_3072,RSA_4096,EC_prime256v1,EC_secp384r1,EC_secp521r1"
        foreach ($cert in (Invoke-AwsJson $Region @("acm", "list-certificates", "--includes", $keyTypes)).CertificateSummaryList) {
            $detail = (Invoke-AwsJson $Region @("acm", "describe-certificate", "--certificate-arn", $cert.CertificateArn)).Certificate
            if (-not $detail) { continue }
            $tags = $null
            try { $tags = (Invoke-AwsJson $Region @("acm", "list-tags-for-certificate", "--certificate-arn", $cert.CertificateArn)).Tags } catch { }
            $detail | Add-Member -NotePropertyName Tags -NotePropertyValue $tags -Force -PassThru
        }
    }
    $files["acm_inuseby_full.json"] = $acm
    $files["acm_inuseby.json"] = if (Test-Failed $acm) { $acm } else { , @($acm | Where-Object { $_.InUseBy -and @($_.InUseBy).Count -gt 0 }) }

    # AWS Config rules + scope / compliance
    $files["config_rules.json"] = Get-Safe {
        $rules = (Invoke-AwsJson $Region @("configservice", "describe-config-rules")).ConfigRules
        $compliance = @{}
        try {
            foreach ($c in (Invoke-AwsJson $Region @("configservice", "describe-compliance-by-config-rule")).ComplianceByConfigRules) {
                $compliance[$c.ConfigRuleName] = $c.Compliance
            }
        } catch { }
        foreach ($r in $rules) {
            $tags = $null
            try { $tags = (Invoke-AwsJson $Region @("configservice", "list-tags-for-resource", "--resource-arn", $r.ConfigRuleArn)).Tags } catch { }
            $r | Add-Member -NotePropertyName Compliance -NotePropertyValue $compliance[$r.ConfigRuleName] -Force
            $r | Add-Member -NotePropertyName Tags -NotePropertyValue $tags -Force -PassThru
        }
    }

    # Network interfaces (all, with attachment / requester info)
    $files["eni_attachments.json"] = Get-Safe { (Invoke-AwsJson $Region @("ec2", "describe-network-interfaces")).NetworkInterfaces }

    # EBS volumes (all + in-use), snapshots by source volume, instances they attach to
    $volumes = Get-Safe { (Invoke-AwsJson $Region @("ec2", "describe-volumes")).Volumes }
    $files["volumes_all.json"] = $volumes
    $files["volumes_live.json"] = if (Test-Failed $volumes) { $volumes } else { , @($volumes | Where-Object { $_.State -eq "in-use" }) }
    $files["snapshots_volumeid.json"] = Get-Safe {
        (Invoke-AwsJson $Region @("ec2", "describe-snapshots", "--owner-ids", "self")).Snapshots | Where-Object { $_.VolumeId }
    }
    $instances = Get-Safe {
        foreach ($res in (Invoke-AwsJson $Region @("ec2", "describe-instances")).Reservations) {
            foreach ($i in $res.Instances) {
                # Oggetto completo + chiavi flat usate da build_volumes_report.py
                $name = ($i.Tags | Where-Object { $_.Key -eq "Name" } | Select-Object -First 1).Value
                $stateDetail = $i.State
                $i | Add-Member -NotePropertyName Name -NotePropertyValue $name -Force
                $i | Add-Member -NotePropertyName StateDetail -NotePropertyValue $stateDetail -Force
                $i | Add-Member -NotePropertyName State -NotePropertyValue $stateDetail.Name -Force -PassThru
            }
        }
    }
    $files["instances_all.json"] = $instances
    if (Test-Failed $instances) {
        $files["instances_for_volumes.json"] = $instances
    } else {
        $attached = @{}
        if (-not (Test-Failed $volumes)) {
            foreach ($v in $volumes) { foreach ($a in $v.Attachments) { $attached[$a.InstanceId] = $true } }
        }
        $files["instances_for_volumes.json"] = @($instances | Where-Object { $attached.ContainsKey($_.InstanceId) })
    }
    return $files
}

if (-not $Account) { $Account = (Invoke-AwsJson $HomeRegion @("sts", "get-caller-identity")).Account }
if ($Regions -eq "all") {
    $RegionList = @((Invoke-AwsJson $HomeRegion @("ec2", "describe-regions")).Regions.RegionName | Sort-Object)
} else {
    $RegionList = @($Regions -split "," | ForEach-Object { $_.Trim() } | Where-Object { $_ })
}
Write-Output "Account ${Account}: $($RegionList.Count) regions -> $($RegionList -join ', ')"

foreach ($Region in $RegionList) {
    $files = Get-RegionEvidence $Region
    $failed = @($files.Values | Where-Object { Test-Failed $_ })
    if ($failed.Count -eq $files.Count) { Write-Output "[$Region] ERROR: $($failed[0].error)"; continue }
    $nonEmpty = @($files.Values | Where-Object { -not (Test-Failed $_) -and @($_).Count -gt 0 })
    if ($nonEmpty.Count -eq 0) { Write-Output "[$Region] no resources, skipped"; continue }

    $OUT = Join-Path (Join-Path $OutRoot $Account) $Region
    New-Item -ItemType Directory -Force $OUT | Out-Null
    foreach ($name in $files.Keys) { Save-Json (Join-Path $OUT $name) $files[$name] }

    # Consolidated volume -> instance / snapshot report
    $reportInputs = @("volumes_all.json", "instances_for_volumes.json", "snapshots_volumeid.json")
    $badInput = $reportInputs | Where-Object { Test-Failed $files[$_] } | Select-Object -First 1
    if ($badInput) {
        Save-Json (Join-Path $OUT "volumes_report.json") ([pscustomobject]@{ error = "volumes_report non generato: $badInput contiene un errore" })
    } else {
        & python $ReportScript $OUT | Out-Null
        if ($LASTEXITCODE -ne 0) { Save-Json (Join-Path $OUT "volumes_report.json") ([pscustomobject]@{ error = "build_volumes_report failed" }) }
    }

    Write-Output "[$Region] written to $OUT"
    Get-ChildItem $OUT -Filter *.json | Select-Object Name, Length | Format-Table -AutoSize | Out-String | Write-Output
}
