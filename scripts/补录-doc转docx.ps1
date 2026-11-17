<#
! DEPRECATED（2026-11-17, P1-5）：本脚本已废弃，勿再使用。
! 功能与 tools\doc_convert.py 重叠且后者已成为生产链路（.doc→.docx 就地转换 + convert_log
! + DB 台账 + 门禁，47 件已全部转换完结）；本脚本与 docs/03 的"30-补录 镜像树"方案
! 一同退役。保留文件仅作历史参考。替代入口：
!   python tools\doc_convert.py          # .doc/.rtf/.ppt → docx（就地转换）
!   python tools\intake.py / classify.py # 入库与分类
.SYNOPSIS
  [废弃] 补录未入库的 .doc：用 Word COM 批量转成 .docx，落到 30-补录 镜像树；可选生成 overrides 初稿。

.DESCRIPTION
  - 只读打开源文件，不改源目录；产物全部写 <SourceRoot>\30-补录，可整目录删除回滚。
  - 幂等：目标 .docx 已存在且大于 1 KB 就跳过。
  - 单个失败不中断，结果写 build\补录日志.json。
  - 额外拷贝：技术面试常见智力题.pdf、计算机组成.md（被目录 skip 规则误伤的两篇）。

.EXAMPLE
  pwsh -File .\补录-doc转docx.ps1 -DryRun
.EXAMPLE
  pwsh -File .\补录-doc转docx.ps1 -EmitOverrides
#>
[CmdletBinding()]
param(
    [string]$SourceRoot = '<CORPUS_ROOT>',
    [string]$StageName  = '30-补录',
    [string]$BuildRel   = '21-面经数据库\build',
    [switch]$EmitOverrides,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'

$srcRoot   = (Get-Item -LiteralPath $SourceRoot).FullName
$stageRoot = Join-Path $srcRoot $StageName
$buildDir  = Join-Path $srcRoot $BuildRel

$excludeTop = @('99-重复文件待删', '20-题库MD', '21-面经数据库', $StageName)
$excludeSeg = @('简历模板')

# ---------- 1. 收集待转换文件 ----------
$targets = New-Object System.Collections.Generic.List[object]
$all = Get-ChildItem -LiteralPath $srcRoot -Recurse -File -ErrorAction Stop
foreach ($item in $all) {
    if ($item.Extension -ne '.doc') { continue }
    $rel = $item.FullName.Substring($srcRoot.Length).TrimStart('\')
    $segs = $rel -split '\\'
    if ($excludeTop -contains $segs[0]) { continue }
    $hit = $false
    foreach ($s in $segs) { if ($excludeSeg -contains $s) { $hit = $true; break } }
    if ($hit) { continue }
    $targets.Add([pscustomobject]@{
        Rel     = $rel
        Full    = $item.FullName
        SizeMB  = [math]::Round($item.Length / 1MB, 2)
    })
}

# 额外直接拷贝的文件（被 skip 规则误伤的正文）
$extras = @(
    '09-算法与数据结构\面试题精华集\技术面试常见智力题.pdf',
    '09-算法与数据结构\面试题精华集\计算机基础\图解计算机组成原理\计算机组成.md'
)

Write-Host ("待转换 .doc: {0} 个；额外拷贝: {1} 个" -f $targets.Count, $extras.Count)

# ---------- 2. Word COM 批量另存 ----------
$word = $null
if (-not $DryRun) {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    try { $word.AutomationSecurity = 3 } catch { }
}

$log = New-Object System.Collections.Generic.List[object]

foreach ($t in $targets) {
    $targetRel = [IO.Path]::ChangeExtension($t.Rel, '.docx')
    $target    = Join-Path $stageRoot $targetRel

    if (Test-Path -LiteralPath $target) {
        $len = (Get-Item -LiteralPath $target).Length
        if ($len -gt 1024) {
            $log.Add([pscustomobject]@{ rel = $t.Rel; ok = 'skip'; target = $targetRel; err = '' })
            continue
        }
    }

    if ($DryRun) {
        Write-Host ("[dry] {0}  ->  {1}" -f $t.Rel, $targetRel)
        continue
    }

    $dir = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }

    $doc = $null
    try {
        $doc = $word.Documents.Open($t.Full, $false, $true)
        $doc.SaveAs2($target, 16)          # 16 = wdFormatXMLDocument (.docx)
        $doc.Close(0)
        $doc = $null
        $log.Add([pscustomobject]@{ rel = $t.Rel; ok = 'ok'; target = $targetRel; err = '' })
    }
    catch {
        if ($null -ne $doc) { try { $doc.Close(0) } catch { } }
        $log.Add([pscustomobject]@{ rel = $t.Rel; ok = 'fail'; target = $targetRel; err = $_.Exception.Message })
    }
}

foreach ($rel in $extras) {
    $from = Join-Path $srcRoot $rel
    if (-not (Test-Path -LiteralPath $from)) {
        $log.Add([pscustomobject]@{ rel = $rel; ok = 'fail'; target = ''; err = '源文件不存在' })
        continue
    }
    $target = Join-Path $stageRoot $rel
    if ($DryRun) { Write-Host ("[dry] copy {0}" -f $rel); continue }
    $dir = Split-Path -Parent $target
    if (-not (Test-Path -LiteralPath $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    Copy-Item -LiteralPath $from -Destination $target -Force
    $log.Add([pscustomobject]@{ rel = $rel; ok = 'ok'; target = $rel; err = 'copy' })
}

if ($null -ne $word) {
    try { $word.Quit() } catch { }
    [void][Runtime.InteropServices.Marshal]::ReleaseComObject($word)
}

# ---------- 3. 写日志 ----------
if (-not $DryRun) {
    if (-not (Test-Path -LiteralPath $buildDir)) { New-Item -ItemType Directory -Path $buildDir -Force | Out-Null }
    $logPath = Join-Path $buildDir '补录日志.json'
    $logJson = $log | ConvertTo-Json -Depth 4
    [IO.File]::WriteAllText($logPath, $logJson, (New-Object Text.UTF8Encoding($false)))
    Write-Host ("日志: {0}" -f $logPath)
}

# ---------- 4. 生成 overrides 初稿 ----------
function Get-TopicByKeyword {
    param([string]$Name)
    if ($Name -match '设计模式') { return '10-设计模式' }
    if ($Name -match 'JVM|垃圾回收|内存区域|分代|类加载') { return '02-JVM' }
    if ($Name -match 'synchronized|CAS|AQS|线程池|volatile|内存模型|并发|阻塞队列|原子性|有序性|可见性|happens-before') { return '03-并发编程' }
    if ($Name -match 'Spring|IOC|AOP|cglib|事务|Bean|SpringBoot|MVC|Cloud') { return '06-Spring框架' }
    if ($Name -match 'HashMap|hash|JDK|集合|异常|String') { return '01-Java基础' }
    if ($Name -match '大厂|真题|面经') { return '11.2-大厂真题' }
    return '12-综合题库'
}

if ($EmitOverrides -and -not $DryRun) {
    $ov = [ordered]@{}
    foreach ($t in $targets) {
        $key = [IO.Path]::ChangeExtension($t.Rel, '.docx')
        $leaf = [IO.Path]::GetFileNameWithoutExtension($t.Rel)
        $topic = Get-TopicByKeyword -Name $leaf
        $ov[$key] = [ordered]@{ category = '题库'; topic = $topic; sub_topic = '' }
    }
    $ovPath = Join-Path $buildDir 'overrides.json'
    $ovJson = $ov | ConvertTo-Json -Depth 5
    [IO.File]::WriteAllText($ovPath, $ovJson, (New-Object Text.UTF8Encoding($false)))
    Write-Host ("overrides 初稿: {0}（{1} 条，请人工复核）" -f $ovPath, $ov.Count)
}

# ---------- 5. 汇总 ----------
$okN   = @($log | Where-Object { $_.ok -eq 'ok' }).Count
$skipN = @($log | Where-Object { $_.ok -eq 'skip' }).Count
$failN = @($log | Where-Object { $_.ok -eq 'fail' }).Count
Write-Host ("完成: ok={0} skip={1} fail={2}" -f $okN, $skipN, $failN)
if ($failN -gt 0) {
    Write-Host '失败项:'
    $log | Where-Object { $_.ok -eq 'fail' } | ForEach-Object { Write-Host ("  {0}  -- {1}" -f $_.rel, $_.err) }
}
Write-Host ("下一步: inventory.py -> convert.py --category 题库 -> gate.py；产物目录 {0}" -f $stageRoot)
