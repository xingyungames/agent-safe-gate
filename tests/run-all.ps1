# =============================================================================
# run-all.ps1 - run every offline acceptance suite in one go.
# Compatible with Windows PowerShell 5.1 and PowerShell 7+.
#
# NOTE: this file is intentionally ASCII-only. Windows PowerShell 5.1 reads
# .ps1 files as ANSI unless they carry a UTF-8 BOM, which corrupts non-ASCII
# text and can even break parsing. Keeping it ASCII avoids that trap entirely.
#
# Design:
#   * Every suite gets its own port, its own data root and its own process, so
#     they cannot pollute each other:
#       - "business" expects a clean workspace (no pre-existing note.txt)
#       - "e2e-biz" is limited to 3 self-registrations per hour per IP
#       - "attacks" bans the local IP; "bruteforce" locks the admin account
#   * The two model-dependent suites (e2e-p0 / e2e-agent) run against the
#     deterministic fake model shipped in tests/mock-ai.mjs - fully offline.
#   * No npm install, no outbound network access.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File tests\run-all.ps1
#   powershell -ExecutionPolicy Bypass -File tests\run-all.ps1 -SkipBuild
#   powershell -ExecutionPolicy Bypass -File tests\run-all.ps1 -Only e2e-p0,e2e-biz
# =============================================================================
param(
    [switch]$SkipBuild,
    [int]$BasePort = 8962,
    [string]$Only = ''
)

$ErrorActionPreference = 'Continue'
# Works both as a script file (powershell -File ...) and via Invoke-Expression
$testsDir  = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$repo      = Split-Path -Parent $testsDir
$project   = Join-Path $repo 'AiApprovalServer'
$exe       = Join-Path $project 'bin\Debug\net8.0\AiApprovalServer.exe'
$mockPort  = $BasePort + 20
$mockKey   = 'sk-mock-key-for-tests-1234567890'

$results = New-Object System.Collections.ArrayList
$script:slot = 0
$wanted = @()
if ($Only) { $wanted = $Only.Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ } }

function Write-Section([string]$title) {
    Write-Host ''
    Write-Host ('=' * 72)
    Write-Host $title
    Write-Host ('=' * 72)
}

function Test-Wanted([string]$name) {
    if ($wanted.Count -eq 0) { return $true }
    return ($wanted -contains $name)
}

if (-not $SkipBuild -or -not (Test-Path $exe)) {
    Write-Host '[build] dotnet build AiApprovalServer ...'
    Push-Location $repo
    dotnet build 'AiApprovalServer\AiApprovalServer.csproj' -c Debug -v minimal | Select-Object -Last 4
    Pop-Location
    if (-not (Test-Path $exe)) {
        Write-Host '[fatal] build output not found: ' + $exe
        exit 1
    }
}

# --------------------------------------------------------------- fake model
$mock = $null
$needsMock = (Test-Wanted 'e2e-p0') -or (Test-Wanted 'e2e-agent')
if ($needsMock) {
    $mock = Start-Process node -ArgumentList 'mock-ai.mjs', $mockPort -PassThru -WindowStyle Hidden -WorkingDirectory $testsDir
    Start-Sleep -Seconds 2
    Write-Host ('[mock] mock-ai.mjs listening on port ' + $mockPort)
}

# --------------------------------------------------------------- start one app instance
function Start-App([string]$dataRoot, [int]$port, [bool]$withAi) {
    $env:AISERVER_DATA_ROOT  = $dataRoot
    $env:AISERVER_LISTEN_URL = 'http://127.0.0.1:' + $port
    if ($withAi) {
        $env:AISERVER_AI_KEY  = $mockKey
        $env:Ai__BaseUrl      = 'http://127.0.0.1:' + $mockPort + '/chat/completions'
        $env:Ai__AllowedHosts = '127.0.0.1'
        $env:Ai__Model        = 'mock-model'
    } else {
        $env:AISERVER_AI_KEY  = ''
        $env:Ai__BaseUrl      = ''
        $env:Ai__AllowedHosts = ''
    }
    # the attack suite needs to survive many strikes before the ban kicks in
    $env:Limits__WafStrikesBeforeBan = '40'

    $p = Start-Process $exe -PassThru -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Milliseconds 700
        try {
            $r = Invoke-WebRequest -Uri ('http://127.0.0.1:' + $port + '/api/public/health') -UseBasicParsing -TimeoutSec 3
            if ($r.StatusCode -eq 200) { return $p }
        } catch { }
    }
    Write-Host ('[warn] service did not become ready on port ' + $port)
    return $p
}

function Stop-App($proc) {
    if ($null -ne $proc) { Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 800
}

# --------------------------------------------------------------- run one suite
# $phase is only used by e2e.mjs (business / attacks / bruteforce / ai)
function Invoke-Suite([string]$name, [string]$scriptFile, [string]$phase, [bool]$withAi, [string[]]$extra) {
    if (-not (Test-Wanted $name)) { return }

    Write-Section ('suite: ' + $name)
    $port     = $BasePort + $script:slot
    $url      = 'http://127.0.0.1:' + $port
    $dataRoot = Join-Path $repo ('_run-' + $name)
    Remove-Item $dataRoot -Recurse -Force -ErrorAction SilentlyContinue
    $script:slot++

    $app = Start-App $dataRoot $port $withAi

    $nodeArgs = @((Join-Path $testsDir $scriptFile))
    if ($phase) { $nodeArgs += $phase }
    $nodeArgs += $url
    if ($extra) { $nodeArgs += $extra }

    Write-Host ('[run] node ' + ($nodeArgs -join ' '))
    $out  = & node @nodeArgs 2>&1
    $code = $LASTEXITCODE
    $out | Select-Object -Last 4

    Stop-App $app
    Remove-Item $dataRoot -Recurse -Force -ErrorAction SilentlyContinue

    $tail = ($out | Select-String -Pattern '通过\s*\d+\s*项|失败\s*\d+\s*项' | Select-Object -Last 1)
    $summary = '(no result line)'
    if ($null -ne $tail) {
        $summary = $tail.Line.Trim()
    } elseif ($code -eq 0) {
        $summary = 'exit 0 (no count line)'
    } else {
        $summary = 'exit ' + $code
    }

    [void]$results.Add([pscustomobject]@{ Suite = $name; Exit = $code; Result = $summary })
}

# model-dependent suites (need a clean data root)
Invoke-Suite 'e2e-p0'          'e2e-p0.mjs'    ''           $true  @()
Invoke-Suite 'e2e-agent'       'e2e-agent.mjs' ''           $true  @()
# model-independent suites (each in its own instance)
Invoke-Suite 'e2e-business'    'e2e.mjs'       'business'   $false @()
Invoke-Suite 'e2e-biz'         'e2e-biz.mjs'   ''           $false @()
Invoke-Suite 'e2e-attacks'     'e2e.mjs'       'attacks'    $false @()
Invoke-Suite 'e2e-bruteforce'  'e2e.mjs'       'bruteforce' $false @()
Invoke-Suite 'e2e-p0-degraded' 'e2e-p0.mjs'    ''           $false @('--degraded')

# static frontend check needs no server
if (Test-Wanted 'frontend-check') {
    Write-Section 'suite: frontend-check'
    & node (Join-Path $testsDir 'frontend-check.mjs') (Join-Path $project 'wwwroot') 2>&1 | Select-Object -Last 3
    $code = $LASTEXITCODE
    $summary = 'frontend-check failed'
    if ($code -eq 0) { $summary = 'frontend-check passed' }
    [void]$results.Add([pscustomobject]@{ Suite = 'frontend-check'; Exit = $code; Result = $summary })
}

if ($null -ne $mock) { Stop-Process -Id $mock.Id -Force -ErrorAction SilentlyContinue }

Write-Section 'summary'
$results | Format-Table -AutoSize
$failed = @($results | Where-Object { $_.Exit -ne 0 })
if ($failed.Count -eq 0) {
    if ($results.Count -eq 0) {
        Write-Host 'Nothing ran (check -Only filter for typos).'
        exit 1
    }
    Write-Host 'All suites passed.'
    exit 0
}
Write-Host ('Failed suites: ' + (($failed | ForEach-Object { $_.Suite }) -join ', '))
exit 1
