$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$issues = New-Object System.Collections.Generic.List[string]

function Add-Issue([string]$Message) {
    $issues.Add($Message)
}

function Is-PlaceholderPhone([string]$Value) {
    return $Value -in @("17000000000", "17000000001", "17000000002")
}

$configPaths = @(
    (Join-Path $root "config.json"),
    (Join-Path $root "config.share.json"),
    (Join-Path $root "config.example.json"),
    (Join-Path $root "src\config.example.json")
)
foreach ($configPath in $configPaths) {
  if (Test-Path -LiteralPath $configPath) {
    $display = $configPath.Substring($root.Length).TrimStart('\')
    try {
        $cfg = Get-Content -Raw -Encoding UTF8 -LiteralPath $configPath | ConvertFrom-Json
        foreach ($buyer in @($cfg.buyers)) {
            if ($buyer.phone -and -not (Is-PlaceholderPhone ([string]$buyer.phone))) {
                Add-Issue "$display 中存在疑似真实手机号：buyers[].phone（已脱敏匹配）"
            }
            if ($buyer.email -and ([string]$buyer.email) -notmatch "(?i)(example\.com|example\.org|example\.net)$") {
                Add-Issue "$display 中存在疑似真实邮箱：buyers[].email（已脱敏匹配）"
            }
        }
        if ($cfg.settings.email) {
            if ($cfg.settings.email.pass) {
                Add-Issue "$display 的 settings.email.pass 非空，可能包含 SMTP 授权码"
            }
            if ($cfg.settings.email.user -and ([string]$cfg.settings.email.user) -notmatch "(?i)(example\.com|example\.org|example\.net)$") {
                Add-Issue "$display 的 settings.email.user 可能包含真实邮箱"
            }
        }
        if ($cfg.settings.qr_cookie) {
            Add-Issue "$display 的 settings.qr_cookie 非空，包含 PC 浏览器凭据"
        }
        if ($cfg.settings.proxy_api_url -and ([string]$cfg.settings.proxy_api_url) -match '(?i)(secret|token|key|orderId)=') {
            Add-Issue "$display 的代理 API 链接可能包含密钥或订单号"
        }
        if ($display -ne "config.json") {
            if ($cfg.settings.auto_submit -or -not $cfg.settings.dry_run) {
                Add-Issue "$display 不是安全的演练模式"
            }
            if ($cfg.settings.debug_checkout) {
                Add-Issue "$display 开启了 debug_checkout，运行后会产生敏感原文"
            }
        }
        foreach ($task in @($cfg.cookies)) {
            $file = [string]$task.cookie_file
            if ($file -and $file -notmatch "示例-请替换") {
                Add-Issue "$display 引用了非示例身份文件（已隐藏文件名）"
            }
        }
    } catch {
        Add-Issue "$display 无法解析：$($_.Exception.Message)"
    }
  }
}

$identityDir = Join-Path $root "身份"
if (Test-Path -LiteralPath $identityDir) {
    Get-ChildItem -LiteralPath $identityDir -File -Filter "*.txt" | ForEach-Object {
        if ($_.Name -eq "示例-请替换.txt" -or $_.Name -like "README*") { return }
        try {
            $raw = Get-Content -Raw -Encoding UTF8 -LiteralPath $_.FullName
            if ($raw -match '"cookie"\s*:\s*"([^"]+)') {
                Add-Issue "身份目录存在非空 cookie 文件：$($_.Name)"
            } else {
                Add-Issue "身份目录存在非模板文本文件：$($_.Name)"
            }
        } catch {
            Add-Issue "身份文件无法读取：$($_.Name)"
        }
    }
}

$ledgerPath = Join-Path $root "orders-ledger.json"
if (Test-Path -LiteralPath $ledgerPath) {
    try {
        $ledger = Get-Content -Raw -Encoding UTF8 -LiteralPath $ledgerPath | ConvertFrom-Json
        if (@($ledger.events).Count -gt 0 -or @($ledger.by_buyer.PSObject.Properties).Count -gt 0 -or @($ledger.by_task.PSObject.Properties).Count -gt 0) {
            Add-Issue "orders-ledger.json 包含历史抢购记录"
        }
    } catch {
        Add-Issue "orders-ledger.json 无法解析"
    }
}

foreach ($dirname in @("logs", "sessions", "支付二维码")) {
    $dir = Join-Path $root $dirname
    if (Test-Path -LiteralPath $dir) {
        $count = @(Get-ChildItem -LiteralPath $dir -Recurse -Force -File).Count
        if ($count -gt 0) {
            Add-Issue "$dirname 目录包含 $count 个运行文件，可能含敏感数据"
        }
    }
}

$ordersPath = Join-Path $root "orders.json"
if (Test-Path -LiteralPath $ordersPath) {
    Add-Issue "orders.json 包含订单号、访客链接或取货信息"
}

$demoCookie = Join-Path $root "二维码支付demo\cookies示例.txt"
if (Test-Path -LiteralPath $demoCookie) {
    $demoRaw = Get-Content -Raw -Encoding UTF8 -LiteralPath $demoCookie
    if ($demoRaw -match 'dssid2=(?!这里替换成你的)[^;\s]+') {
        Add-Issue "二维码支付demo\cookies示例.txt 疑似包含真实浏览器 Cookie"
    }
}

if ($issues.Count -eq 0) {
    Write-Host "检查通过：未发现需要阻止分享的个人数据或凭据。" -ForegroundColor Green
    exit 0
}

Write-Host "发现以下风险，不建议直接分享：" -ForegroundColor Red
$issues | ForEach-Object { Write-Host " - $_" -ForegroundColor Yellow }
Write-Host ""
Write-Host "建议重新使用原始空白包，或删除上述敏感文件后再打包。" -ForegroundColor Yellow
exit 1
