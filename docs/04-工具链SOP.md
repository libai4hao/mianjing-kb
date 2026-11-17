# 工具链 SOP

面向"下一轮直接照着跑"，每个脚本都给出命令、参数语义、输出解读和排障。

环境：`python`（3.11.4，含 pymupdf / pymupdf4llm / numpy / sklearn / yaml / pytest）。
Shell：pwsh 7（`C:\Program Files\PowerShell\7\pwsh.exe`）。所有中文路径用 `-LiteralPath`。

---

## 0. 一次性准备

```powershell
$py = "python"
$repo = "<REPO_ROOT>"
$db = "<CORPUS_ROOT>\21-面经数据库\build\kb.sqlite"
Set-Location -LiteralPath $repo
```

---

## 1. intake.py —— 登记 + 判重

```powershell
& $py tools\intake.py --db $db                 # 全量登记语料根
& $py tools\intake.py --db $db --dry-run       # 只统计（有库时也报真实重复数）
& $py tools\intake.py --db $db --root "<DOWNLOAD_DIR>\面试资料"   # 语料树之外的目录
& $py tools\intake.py --db $db --all-exts      # 连 png/xmind 也登记（默认只登记文档型）
```

**动作语义**（输出 JSON 的 `actions`）：

| 动作 | 含义 | 处理 |
|---|---|---|
| `added` | 新哈希 | 插 `pending_classify` |
| `duplicate` | 同哈希、同路径 | 只刷 `last_seen_at` |
| `renamed` / `moved` | 同哈希、不同路径 | 更新路径，**状态不变**（不降级已入库成果） |
| `resurrected` | 该哈希曾 `absent` 又回来 | 复用原分类结果，回到 `classified` |
| `to_quarantine` | 正式区记录被移进 99 区（过时件清理） | 改路径 + 状态转 `quarantined`，立刻退出清单 |
| `quarantined` | `99-重复文件待删` 下 | 只登记，永不处理 |
| `ignored` | 非文档型扩展名 | 只计数 |

**排障**：
- 同一批内出现两份相同文件 → 现在会当场判 `duplicate`（D-11）；若表行数 < added 数，说明旧版本代码还在用"最后统一写入"。
- 读写失败会打印 `[warn] 读取失败 …` 并计入 `error`，不静默跳过。

---

## 2. classify.py —— 先分类

```powershell
& $py tools\classify.py --db $db                  # 只判 pending_classify
& $py tools\classify.py --db $db --redo           # 改规则后重判（跳过 quarantined/absent/superseded）
& $py tools\classify.py --db $db --stats          # 看分布
& $py tools\classify.py --db $db --show 40        # 抽查前 40 条
& $py tools\classify.py --db $db --dry-run        # 不写库，只看会怎么判
```

判定链（`meta/folder_rules.yaml` + `meta/topic_rules.yaml` + `meta/overrides.json`）：

0. `meta/overrides.json` 人工覆盖表最高优先级（P1-5）：key=语料树内 posix 相对路径（兼容反斜杠），value={category/topic/sub_topic/source_trust/note}，命中即 topic_source=override，不再走规则；
1. `30-补录/` 前缀先剥掉（镜像暂存树，目录语义继续生效）；
2. `category` / `source_trust`：结构位规则（`11.1` 面经 low、`11.2` 真题 high、`11.3` 专题笔记 low）→ 顶层主题目录（`01-`~`10-`、`12-` → 题库 medium）→ 默认未分类；
3. `topic`：文件名 regex（30 条，大小写不敏感）→ 顶层目录映射 → 未分类；
   面经/真题/专题笔记若未命中领域，落 **`综合`**（诚实答案，不是缺分类）；
4. `collection`：目录路径剔除包装词（`Markdown版`、`原创PDF面试手册`…）与**主题名**（MySQL/JVM/多线程…）后最深一级；全被剔光则退回最深一级。

**注意**：`collection` 剔除主题名是为了避免把 `12-综合题库\Markdown笔记\MySQL\` 的几十份不同出处资料错绑成一个"来源"。

---

## 3. gate.py —— 文本质量门禁

```powershell
& $py tools\gate.py --md-dir "<CORPUS_ROOT>\20-题库MD" --db $db --top 12
& $py tools\gate.py --md-dir ... --no-db --out build\gate_report.json
```

判定顺序（乱码 → CJK → 内容量，因为字体编码损坏会同时拖垮三个指标）：

| verdict | 判据 | 处置 |
|---|---|---|
| `garbled` | 乱码率 > 0.01 或有效字符率 < 0.90 | 走 OCR |
| `low_cjk` | CJK 占比 < 0.15 | 走 OCR |
| `non_content` | 可见字符 < 800 | 判非内容，不入题库 |
| `pass` | 其余 | 正常入库 |

**2026-11-17 实测**：397 个 md → pass 338 / non_content 41 / garbled 13 / low_cjk 5；
捕获的正是已知的"假成功"（JavaGuide v1.0 0.060、V1.1 0.055、v2.0 0.065、Leetcode-Go 0.139、
Nginx 开发手册 0.092、剑指Offer 0.062、大厂算法合集 0.135、数据结构面试题 0.046、操作系统面试题 0.086）。

---

## 4. ocr_pages.py —— 页级 OCR

```powershell
& $py tools\ocr_pages.py --smoke                                  # 引擎自检（实测 init 0.99 s）
& $py tools\ocr_pages.py --pdf "x.pdf" --pages 1-3 --dpi 200      # 渲染 + OCR + 打印
```

**硬约束**（踩过的坑）：
- 进程 cwd 必须是引擎目录；
- 启动后等 `OCR init completed.`，不能立刻发请求；
- 图片路径必须 **ASCII**（渲染落到 `%TEMP%\kbocr_*`，中文路径引擎直接拒）；
- 单页 0.72–0.80 s，score 多 0.92+。

---

## 5. 测试

```powershell
& $py -m pytest tests -q
```

32 个用例覆盖：文本指标与阈值、intake 的新增/重复/改名/复活/同批重复/移入隔离区/派生树排除、classify 的 topic/结构位/collection/未分类、gate 的三种 verdict 与报告落盘、bookmarks 的段区间/章节不误判/落库。

---

## 6. ingest_queue.py —— 可入库清单（文件表）

视图 `ingest_queue` = `status='classified'`（已排除 `rejected` 非内容与 `quarantined` 隔离区件）。
清单字段：`doc_hash, file_name, parent_dir(父目录名), dir_path, ext, size_bytes, category, topic, collection, source_trust`。

```powershell
& $py tools\ingest_queue.py --db $db --export "<CORPUS_ROOT>\21-面经数据库\build\ingest_list.csv"
& $py tools\ingest_queue.py --db $db --stats
& $py tools\ingest_queue.py --db $db --lookup "3afb55d83ce320e0"      # 按 hash
& $py tools\ingest_queue.py --db $db --lookup "设计模式"                # 按文件名片段
```

当前基线：**475 行，hash 唯一 475**；父目录 top：`12-综合题库` 49、`高频面试题-笔记资料01-34` 39、
`面向字节系列` 33、`这是Java面试题资料合集` 21、`06-Spring框架` 22。

用途：① 人眼核对这一批到底入哪些；② 快速去重——新文件先按 hash 比对这张表。

---

## 6.5 bookmarks.py —— 合并版"源边界"落库（删 99 区的硬 gate）

合并轮把 110 个源并入 23 份 `【合并版】*.pdf`，md 侧已无源边界（0 个一级标题、源名无痕迹），
唯一载体是 PDF TOC 的 L1 书签。删除 `99-重复文件待删` **之前必须先跑本脚本**（ADR D-16 / D-17）。

```powershell
& $py tools\bookmarks.py --dry-run    # 先看统计
& $py tools\bookmarks.py              # 增量：只补 source_count 为空的
& $py tools\bookmarks.py --force      # 全部重扫（合并版重做过才需要）
```

产出 `source_documents.source_count` 与 `sources_json`：
`[{"name":"51道.pdf","page_start":1,"page_end":23}, ...]`（页号 1-based，闭区间）。

**判据（踩过的坑）**：源边界只认"**L1 标题形如文件名**"（`.pdf/.docx/.doc/.md/.pptx/.txt` 结尾）。
普通 PDF 的 L1 是章节标题，若把"L1 条数"一律当源数，291 份 PDF 会被算成 119 份合并版 / 3,338 个源。
无源级书签的按单源处理并写 `from: fallback_no_source_toc`，不静默。

实测（2026-11-17）：291 份 PDF / **合并版 23 份 / 源 110 个** / 单源 268 / 1.9 s；
JVM 合集 11 段（p1–23, 24–56, 57–80, 81–95, 96–105, 106–134, 135–144, 145–154, 155–174, 175–194, 195–214），
Mongo 4 段（p1–6, 7–10, 11–17, 18–23），与审核时的页数校验完全一致。

---

## 7. pipeline.py —— 批增一条命令
```powershell
& $py tools\pipeline.py              # 处理 31-收件箱：去重登记 → 分类 → 清单
& $py tools\pipeline.py --check      # 只预览，不写库（先用这个确认重不重）
& $py tools\pipeline.py --root "<DOWNLOAD_DIR>\新面经"    # 任意目录
& $py tools\pipeline.py --export "…\build\ingest_list.csv"
```

内部就是串 `intake.py --root <dir>` → `classify.py --status pending_classify` → `ingest_queue.py`。
任一步失败会打印失败脚本与 stderr 尾部，不静默继续。

**`dir_path` 一律相对语料根（ADR D-20）**：`intake` 的 rel 基准是 `scan_base(root)` ——
root 落在语料根内时（如默认的 `31-收件箱`）仍写全 `31-收件箱/面经/…`，root 在语料外才等于自身。
否则结构位规则丢了前缀就永不匹配（实测整批 771 件判成未分类）。

**收件箱按内容性质建子目录**：`31-收件箱/{面经|笔试|面试题}/<批次日期-来源>/<来源子路径>`，
批次目录名不带语义；三条结构位规则钉在这三个目录（面经→`面经`/low、笔试→`真题`/high、
面试题→`题库`/medium）。`31-收件箱/**`→`未分类` 的兜底仍留最后。

**判重口径**（本轮修掉的两个真实缺陷，见 ADR D-12）：

| 情况 | 判定 | 为什么 |
|---|---|---|
| 新哈希 | `added` | 插 `pending_classify` |
| 同哈希、同路径 | `duplicate` | 只刷 `last_seen_at` |
| 同哈希、原件**仍在**磁盘 | `duplicate` | 这是副本；**不许**把库里那条记录的位置改走 |
| 同哈希、原件已不在原处 | `moved`/`renamed` | 真移动/改名，改路径但**状态不降级** |
| 正式区原件已被移进 `99-重复文件待删` | `to_quarantine` | 改路径 + 状态 `quarantined`；原件仍在磁盘时仍只算 `duplicate`（杜绝散副本夺主，D-12） |
| `99-重复文件待删` 下 | `quarantined`，若哈希已存在则 `duplicate` | 隔离区永不入库、永不夺主 |
| `00-索引` / `20-题库MD` / `21-面经数据库`（**任意层级**） | 不登记 | 索引与派生树永不作为源文档；否则 99 区镜像的派生 md 会被当成新源 |
| 曾 `absent` 又回来 | `resurrected` | 复用原分类结果 |

---

## 7.5 doc_convert / purge_origin / question_freq —— 收尾三件
`powershell
# .doc → .docx（Word COM；拒收位里的 .doc 不转），转完原 .doc 记录标 superseded
& $py tools\doc_convert.py                 # 先看多少件
& $py tools\doc_convert.py --apply
& $py tools\intake.py; & $py tools\classify.py    # 新 .docx 走正常登记+分类

# 删导入源里的原件：hash 在库 + 状态已入库 + 保留副本存在且在语料根内 + 路径在 --roots 白名单
& $py tools\purge_origin.py --roots "<RESOURCE_ROOT>\就业资料" "<DOC_ROOT>\待分类"
& $py tools\purge_origin.py --roots … --apply --rmdirs     # 明细写 build\origin_deleted.csv

# 同题频次：抽文本 → 按题号切题 → SimHash 候选 + Jaccard 复核 → 按"出现的文档数"排序
& $py tools\question_freq.py --dirs "31-收件箱\面试题" "31-收件箱\笔试" `
      --csv "…\build\question_freq.csv" --md "docs\07-题目频次报告.md"
`

**判据与坑**：
- purge_origin 四条判据缺一不删；--rmdirs 只清空目录，不清非空目录。
- question_freq 的 SimHash **只当候选**：短问句抖动大（同义改写实测差 24 位、无关题也能进 12 位内），
  真正合并靠 2-gram Jaccard ≥ 0.7 —— 这是**精度优先**，真·同义改写（0.42）会被放过，不误并。
- 切题必须用"答案/解析/说明"截断，否则整段答案会被算成一道题（实测 583 块里大半是答案）。
## 8. 未实现清单（明确记录，不假装有）

| 组件 | 状态 | 缺口 |
|---|---|---|
| `classify --neighbor` | 未实现 | 需文档向量索引（第②档归因，覆盖规则之后的残渣） |
| `cluster`（增量吸附） | 未实现 | `canonical_questions` / `question_variants` 表已建，0.92/0.85 阈值算法未写 |
| `reconcile`（缺口对账） | ✅ 已实现（P1-2，路径级 convert_log vs 20-题库MD） | 磁盘 / 表 / convert_log / DB 四数闭合，用来清 `ok=423 vs 397` 的 26 条旧账 |
| `dispatch.py` | 未实现 | 按 category 选拆题策略（题库→parse_banks，面经→extract，专题→只索引） |
| `extract.py` | 未实现 | 面经抽题 + 公司/年份归一 |
| `30-补录` / `31-收件箱` | `31-收件箱` 已建（空）；`30-补录` 未建 | 补录脚本只跑过 `-DryRun`（41 个 doc 命中） |

---

## 9. 已知陷阱

| 现象 | 原因 | 对策 |
|---|---|---|
| `partially initialized module 'inspect'` | `%TEMP%` 根有 zcode 残留 `inspect.py` | 临时脚本放独立子目录（如 `%TEMP%\zocr\`） |
| `Copy-Item` 找不到中文路径文件 | PowerShell 直接写中文路径 | 先 `Get-ChildItem \| Where-Object { $_.Name -like ... }` 取 `FullName` 再操作 |
| sqlite 写入报 locked | WAL 下多写者 | 单写者串行；本仓库脚本都是短事务 |
| 分类判错 | 规则词典（`meta/*.yaml`）而非数据 | 改 yaml → `classify --redo`，**不碰 md、不重跑 OCR** |
