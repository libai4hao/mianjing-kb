param(
    [ValidateSet('prepare', 'run', 'label', 'report')]
    [string]$Stage = 'prepare',
    [string]$Config = 'D:\pilot\5000\config.json',
    [switch]$WithLabel
)

$ErrorActionPreference = 'Stop'
$repo = Split-Path -Parent $PSScriptRoot
$python = 'D:\dev\python\python.exe'

if (-not (Test-Path -LiteralPath $python)) {
    throw "Python 不存在：$python；请改脚本里的 `$python 或安装到该路径"
}
Set-Location -LiteralPath $repo

$argsList = @('-m', 'mianjing_kb.pilot5000', $Stage, '--config', $Config)
if ($Stage -eq 'run' -and $WithLabel) {
    $argsList += '--with-label'
}
& $python @argsList
exit $LASTEXITCODE
