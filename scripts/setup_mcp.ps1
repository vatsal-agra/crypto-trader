# =============================================================================
# TradingView MCP — Windows Setup Script
# Installs and configures the TradingView MCP server for Claude Code.
# Run from PowerShell (does NOT require Administrator).
# =============================================================================

$ErrorActionPreference = "Stop"
$HOME_DIR   = $env:USERPROFILE
$MCP_DIR    = Join-Path $HOME_DIR "tradingview-mcp"
$CLAUDE_DIR = Join-Path $HOME_DIR ".claude"

Write-Host "`n=== TradingView MCP Setup ===" -ForegroundColor Cyan

# ---------------------------------------------------------------------------
# Step 1 — Clone or update the MCP server
# ---------------------------------------------------------------------------
Write-Host "`n[1/5] Setting up tradingview-mcp repository..." -ForegroundColor Yellow

if (Test-Path (Join-Path $MCP_DIR ".git")) {
    Write-Host "     Found existing repo — pulling latest changes..."
    git -C $MCP_DIR pull
} else {
    Write-Host "     Cloning from GitHub..."
    git clone https://github.com/tradesdontlie/tradingview-mcp.git $MCP_DIR
}

# ---------------------------------------------------------------------------
# Step 2 — Install npm dependencies
# ---------------------------------------------------------------------------
Write-Host "`n[2/5] Installing npm dependencies..." -ForegroundColor Yellow
npm install --prefix $MCP_DIR

# ---------------------------------------------------------------------------
# Step 3 — Create Claude config directory and merge mcp.json
# ---------------------------------------------------------------------------
Write-Host "`n[3/5] Configuring Claude Code MCP entry..." -ForegroundColor Yellow

if (!(Test-Path $CLAUDE_DIR)) {
    New-Item -ItemType Directory -Path $CLAUDE_DIR | Out-Null
}

$MCP_CONFIG_PATH = Join-Path $CLAUDE_DIR "mcp.json"
$SERVER_JS       = Join-Path $MCP_DIR "src\server.js"

$NEW_ENTRY = @{
    command = "node"
    args    = @($SERVER_JS)
}

if (Test-Path $MCP_CONFIG_PATH) {
    $cfg = Get-Content $MCP_CONFIG_PATH -Raw | ConvertFrom-Json
    if ($null -eq $cfg.mcpServers) {
        $cfg | Add-Member -NotePropertyName "mcpServers" -NotePropertyValue ([PSCustomObject]@{}) -Force
    }
    $cfg.mcpServers | Add-Member -NotePropertyName "tradingview" -NotePropertyValue $NEW_ENTRY -Force
    $cfg | ConvertTo-Json -Depth 10 | Set-Content $MCP_CONFIG_PATH -Encoding UTF8
    Write-Host "     Merged tradingview entry into existing mcp.json"
} else {
    @{
        mcpServers = @{
            tradingview = $NEW_ENTRY
        }
    } | ConvertTo-Json -Depth 10 | Set-Content $MCP_CONFIG_PATH -Encoding UTF8
    Write-Host "     Created mcp.json"
}

# ---------------------------------------------------------------------------
# Step 4 — Pre-approve TradingView tools in settings.json
# ---------------------------------------------------------------------------
Write-Host "`n[4/5] Updating Claude Code permissions..." -ForegroundColor Yellow

$SETTINGS_PATH = Join-Path $CLAUDE_DIR "settings.json"
$TV_PERMISSION = "mcp__tradingview__*"

if (Test-Path $SETTINGS_PATH) {
    $settings = Get-Content $SETTINGS_PATH -Raw | ConvertFrom-Json
    if ($null -eq $settings.permissions) {
        $settings | Add-Member -NotePropertyName "permissions" -NotePropertyValue @{ allow = @() } -Force
    }
    if ($settings.permissions.allow -notcontains $TV_PERMISSION) {
        $settings.permissions.allow += $TV_PERMISSION
    }
    $settings | ConvertTo-Json -Depth 10 | Set-Content $SETTINGS_PATH -Encoding UTF8
    Write-Host "     Updated existing settings.json"
} else {
    @{
        permissions = @{
            allow = @($TV_PERMISSION)
        }
    } | ConvertTo-Json -Depth 10 | Set-Content $SETTINGS_PATH -Encoding UTF8
    Write-Host "     Created settings.json"
}

# ---------------------------------------------------------------------------
# Step 5 — Copy rules.json into the MCP directory
# ---------------------------------------------------------------------------
Write-Host "`n[5/5] Deploying trading rules..." -ForegroundColor Yellow

$RULES_SRC = Join-Path $PSScriptRoot "..\config\rules.json"
$RULES_DST = Join-Path $MCP_DIR "rules.json"

if (Test-Path $RULES_SRC) {
    Copy-Item $RULES_SRC $RULES_DST -Force
    Write-Host "     Copied config/rules.json -> $RULES_DST"
} else {
    Write-Host "     WARNING: config/rules.json not found — skipping." -ForegroundColor DarkYellow
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host "`n=== Setup Complete ===" -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Cyan
Write-Host "  1. Fully quit and restart Claude Code"
Write-Host "  2. Open TradingView Desktop"
Write-Host "  3. Inside Claude Code run:  tv_health_check"
Write-Host "  4. Expected: { cdp_connected: true, api_available: true }"
Write-Host ""
