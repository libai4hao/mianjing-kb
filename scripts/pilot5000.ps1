param(
    [ValidateSet('prepare', 'run', 'label', 'report')]
    [string]$Stage = 'prepare',
    [string]$Config = $env:PILOT_CONFIG,
    [string]$Python = $env:PYTHON,
    [switch]$WithLabel
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = if ($Python) { $Python } else { (Get-Command python -ErrorAction Stop).Source }

if (-not $Config) {
    throw "请用 -Config 或环境变量 PILOT_CONFIG 指定配置路径"
}
if (-not (Test-Path -LiteralPath $python)) {
    throw "Python 不存在：$python；请用 -Python 或环境变量 PYTHON 指定"
}
Set-Location -LiteralPath $repo

$argsList = @('-m', 'mianjing_kb.pilot5000', $Stage, '--config', $Config)
if ($Stage -eq 'run' -and $WithLabel) {
    $argsList += '--with-label'
}
& $python @argsList
exit $LASTEXITCODE
