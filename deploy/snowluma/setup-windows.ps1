# setup-windows.ps1 -- Windows side preparation for the containerised SnowLuma.
#
# What it does:
#   1. Allows inbound TCP 8085 (run.py reverse WebSocket) from WSL / LAN.
#      Without this rule the container cannot reach the Windows-side bot.
#   2. Checks whether 8085 is already occupied by another process.
#   3. Prints the candidate URLs to paste into SnowLuma WebUI.
#   4. Sanity-checkes the bot directory and shows whether it is visible in WSL.
#
# Usage (run in an elevated PowerShell - "Run as administrator"):
#   powershell -ExecutionPolicy Bypass -File D:\Code\JungJingNew\deploy\snowluma\setup-windows.ps1
#
# Rollback:
#   powershell -ExecutionPolicy Bypass -File ...\setup-windows.ps1 -Remove

param(
    [switch]$Remove,
    [int]$Port = 8085
)

$ErrorActionPreference = 'Stop'

# ---------- read .env (single source of truth) ----------
$BotWin   = "D:\Code\JungJingNew"
$BotMount = "/mnt/d/Code/JungJingNew"
$EnvFile  = Join-Path $PSScriptRoot ".env"
if (Test-Path $EnvFile) {
    foreach ($line in Get-Content $EnvFile) {
        if ($line -match '^\s*REVERSE_WS_PORT\s*=\s*(\d+)' -and -not $PSBoundParameters.ContainsKey('Port')) {
            $Port = [int]$Matches[1]
        }
        if ($line -match '^\s*BOT_WIN_DIR\s*=\s*(.+?)\s*$')   { $BotWin   = $Matches[1].Trim('"') }
        if ($line -match '^\s*BOT_MOUNT\s*=\s*(.+?)\s*$')     { $BotMount = $Matches[1].Trim('"') }
    }
    Write-Host "    (read settings from $EnvFile)" -ForegroundColor DarkGray
}

$RuleName = "JungJing SnowLuma OneBot WS $Port"

function Write-Step($text) { Write-Host "==> $text" -ForegroundColor Cyan }
function Write-Ok($text)   { Write-Host "    OK   $text" -ForegroundColor Green }
function Write-Warn($text) { Write-Host "    WARN $text" -ForegroundColor Yellow }
function Write-Bad($text)  { Write-Host "    FAIL $text" -ForegroundColor Red }

# ---------- admin check ----------
$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Bad "This script must run as Administrator (needed for firewall rules)."
    Write-Host "    Right-click PowerShell -> 'Run as administrator', then run again."
    exit 1
}

# ---------- remove mode ----------
if ($Remove) {
    Write-Step "Removing firewall rule '$RuleName'"
    $existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($existing) {
        Remove-NetFirewallRule -DisplayName $RuleName
        Write-Ok "Rule removed."
    } else {
        Write-Warn "Rule not found, nothing to do."
    }
    exit 0
}

# ---------- firewall rule ----------
Write-Step "Inbound firewall rule for TCP $Port"
$existing = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Ok "Rule already exists; refreshing it."
    Set-NetFirewallRule -DisplayName $RuleName -Enabled True -Action Allow -Profile Any -Direction Inbound
    $existing | Get-NetFirewallPortFilter | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
} else {
    New-NetFirewallRule `
        -DisplayName $RuleName `
        -Direction Inbound `
        -Action Allow `
        -Protocol TCP `
        -LocalPort $Port `
        -Profile Any `
        -Description "Allow the SnowLuma container (WSL) to reach the Windows-side OneBot bot." | Out-Null
    Write-Ok "Rule created."
}

# ---------- port occupancy ----------
Write-Step "Port $Port usage on Windows"
$listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listeners) {
    foreach ($l in $listeners) {
        $procId = $l.OwningProcess
        $proc = Get-Process -Id $procId -ErrorAction SilentlyContinue
        $pname = if ($proc) { $proc.ProcessName } else { "pid $procId" }
        if ($l.LocalAddress -eq '0.0.0.0' -or $l.LocalAddress -eq '::') {
            Write-Ok "Listening on $($l.LocalAddress):$Port by $pname (all interfaces - good, container can reach it)."
        } else {
            Write-Warn "Listening on $($l.LocalAddress):$Port by $pname - loopback only."
            Write-Host "         The container CANNOT reach 127.0.0.1 on Windows." -ForegroundColor Yellow
            Write-Host "         Set ws_reverse_host to 0.0.0.0 in config/config.json and restart run.py." -ForegroundColor Yellow
        }
    }
} else {
    Write-Warn "Nothing is listening on $Port yet. Start run.py (python run.py) to open the reverse WS server."
}

# ---------- addresses ----------
Write-Step "Candidate URLs for SnowLuma WebUI -> reverse WebSocket"
Write-Host "    1) ws://host.docker.internal:$Port/   <- default, works for WSL NAT + Docker Desktop"
$wslAdapter = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.InterfaceAlias -like '*WSL*' } |
    Select-Object -First 1
if ($wslAdapter) {
    Write-Host "    2) ws://$($wslAdapter.IPAddress):$Port/   <- fallback: direct WSL host address"
} else {
    Write-Warn "No 'vEthernet (WSL)' adapter found. Start WSL once, then re-run to see the fallback address."
}
$lanIp = Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notlike '*WSL*' -and $_.InterfaceAlias -notlike '*Loopback*' } |
    Select-Object -First 1
if ($lanIp) {
    Write-Host "    3) ws://$($lanIp.IPAddress):$Port/   <- last resort: LAN address (needs firewall rule, already added)"
}

# ---------- WSL / docker visibility ----------
Write-Step "WSL and Docker visibility"
$wslExe = "C:\Windows\System32\wsl.exe"
if (-not (Test-Path $wslExe)) {
    Write-Bad "wsl.exe not found. Install WSL first: wsl --install"
} else {
    $distros = & $wslExe -l -q 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $distros) {
        Write-Bad "No WSL distribution found. Run: wsl --install -d Ubuntu"
    } else {
        Write-Ok ("Distributions: " + (($distros | Where-Object { $_.Trim() }) -join ', '))
        $dockerVer = & $wslExe -e bash -lc "docker version --format '{{.Server.Version}}' 2>/dev/null" 2>$null
        if ($LASTEXITCODE -eq 0 -and $dockerVer) {
            Write-Ok "Docker daemon reachable in WSL (server $dockerVer)."
        } else {
            Write-Bad "Docker not reachable inside WSL. Install it, or start Docker Desktop."
        }
        $composeVer = & $wslExe -e bash -lc "docker compose version --short 2>/dev/null" 2>$null
        if ($LASTEXITCODE -eq 0 -and $composeVer) {
            Write-Ok "docker compose available ($composeVer)."
        } else {
            Write-Bad "docker compose plugin missing in WSL."
        }
    }
}

# ---------- bot dir ----------
Write-Step "Bot directory"
if (Test-Path (Join-Path $BotWin "run.py")) {
    Write-Ok "Found $BotWin\run.py"
} else {
    Write-Bad "run.py not found under $BotWin - adjust BOT_WIN_DIR / BOT_MOUNT in .env"
}

Write-Host ""
Write-Host "Next: cd $BotMount/deploy/snowluma && docker compose up -d" -ForegroundColor White
Write-Host "Then open http://127.0.0.1:6081/ (noVNC) and scan the QQ QR code." -ForegroundColor White
