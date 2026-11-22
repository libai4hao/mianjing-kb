# 安装仓库内 git 钩子（privacy_scan pre-push）到当前仓库的 .git/hooks。
#
# 用法：
#   pwsh -File scripts/hooks/install.ps1
#   pwsh -File scripts/hooks/install.ps1 -Python <解释器绝对路径>
#   powershell -File scripts/hooks/install.ps1        # Windows PowerShell 5.1 同样可用
#
# -Python 不传时按 PATH 里的 python / py 自动探测（要求能 import mianjing_kb）；
# 传了就写进 .git/hooks/pre-push-python（仅本机、不入库），钩子优先用它。
# 找不到可用解释器时钩子只告警不拦推送，避免把提交卡死。
param(
    [string]$Python = ''
)

$ErrorActionPreference = 'Stop'

$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$gitDir = Join-Path $repo '.git'
if (-not (Test-Path -LiteralPath $gitDir -PathType Container)) {
    throw "不是 git 仓库：$repo"
}

$src = Join-Path $PSScriptRoot 'pre-push'
$dst = Join-Path $gitDir 'hooks\pre-push'
$hintFile = Join-Path $gitDir 'hooks\pre-push-python'

$text = [System.IO.File]::ReadAllText($src)
# 钩子由 git 的 sh 执行：必须去掉 BOM、统一成 LF，否则 Windows 上会报 bad interpreter
$text = $text.TrimStart([char]0xFEFF) -replace "`r`n", "`n"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($dst, $text, $utf8NoBom)
Write-Host "已安装：$dst"

function Test-Python([string]$exe) {
    if (-not $exe) { return $false }
    $resolved = $null
    try {
        $resolved = (Get-Command $exe -ErrorAction Stop).Source
    } catch {
        if (Test-Path -LiteralPath $exe -PathType Leaf) { $resolved = $exe }
    }
    if (-not $resolved) { return $false }
    & $resolved -c 'import mianjing_kb' 2>$null | Out-Null
    if ($LASTEXITCODE -eq 0) { return $resolved }
    return $false
}

$found = Test-Python $Python
if (-not $found -and -not $Python) {
    foreach ($cand in @('python', 'py')) {
        $found = Test-Python $cand
        if ($found) { break }
    }
}

if ($found) {
    [System.IO.File]::WriteAllText($hintFile, $found, $utf8NoBom)
    Write-Host "钩子解释器：$found"
} else {
    if (Test-Path -LiteralPath $hintFile) { Remove-Item -LiteralPath $hintFile -Force }
    Write-Host "没找到能 import mianjing_kb 的解释器；钩子会只告警不拦。可用 -Python 指定。"
}

Write-Host "验证：git push（或直接跑 sh .git/hooks/pre-push）看是否输出 [scan] N 个文件。"
