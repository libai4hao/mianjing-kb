# vibe-work

面经题库知识库工程的**代码与文档仓库**。语料与数据留在 `<CORPUS_ROOT>`（按方案不搬迁），
本仓库只放可版本化的部分：方案文档、元数据规则、管线脚本、测试。

> 仓库根为 `<REPO_ROOT>`（父目录 `vibe-work` 是共用工作区，另有 `公开课视频转文档`
> 等无关项目，不在本仓库内）。此前约定是"新项目统一放 `<REPO_BASE>`"，这里属于**有意识偏离**，
> 原因见 `docs/00-决策记录.md` D-09。

## 目录

```
docs/                     方案与决策（**06 部署方案 v2-R2 为当前口径** / 01 部署方案 v2-R1 历史稿 / 02 审核报告 / 03 未分类入库方案 / 04 工具链 SOP / 05 合并轮审核）
meta/
  001_init.sql            原文档表 schema（source_documents / batches / gate_results / ocr_pages）
  002_parent_dir.sql      parent_dir（父目录名）+ 可入库清单视图 ingest_queue
  topic_rules.yaml        文件名 → topic（30 条，源自语料内 inventory.py）
  folder_rules.yaml       目录结构位 → category / source_trust；包装词；顶层主题目录；reject_patterns
tools/
  kb/                     公共库：config / db / hashing / textmetrics
  intake.py               登记 + sha256 判重（新增/重复/移动/改名/复活/隔离）
  classify.py             先分类：规则判类、collection、source_trust、未分类兜底、非内容拒收
  gate.py                 文本质量门禁（CJK / 乱码 / 有效字符）
  ocr_pages.py            PaddleOCR-json 页级 OCR（渲染 → OCR → 落库）
  ingest_queue.py         可入库清单：导出 CSV / 统计 / 按 hash 与文件名查重
  pipeline.py             批增一条命令：去重 → 分类 → 清单（`--check` 只预览）
  question_freq.py        同题频次统计：抽文本 → 切题 → SimHash+Jaccard 聚簇 → 文档频率
  doc_convert.py          `.doc` → `.docx`（Word COM），原 `.doc` 记录标 superseded
  purge_origin.py         删除已收入语料的原件（逐件校验 hash + 保留副本存在）
scripts/
  补录-doc转docx.ps1       Word COM 批量 .doc → .docx（落到 30-补录 镜像树，不动源目录）
tests/                    36 个用例（textmetrics / intake / classify / gate / 清单 / 切题）
```

## 快速开始

```powershell
$py = "python"
$repo = "<REPO_ROOT>"
$db = "<CORPUS_ROOT>\21-面经数据库\build\kb.sqlite"
Set-Location -LiteralPath $repo

# 1. 登记（建表 + hash 判重；写库）
& $py tools\intake.py --db $db

# 2. 分类（先分类，再处理）
& $py tools\classify.py --db $db            # 判 pending_classify 的记录
& $py tools\classify.py --db $db --stats    # 看分布
& $py tools\classify.py --db $db --redo     # 改规则后重判（不动 quarantined）

# 3. 门禁（检查 md 产物的文本质量）
& $py tools\gate.py --md-dir "<CORPUS_ROOT>\20-题库MD" --db $db --top 12

# 4. OCR（本地引擎，零 API 成本）
& $py tools\ocr_pages.py --smoke
& $py tools\ocr_pages.py --pdf "某个.pdf" --pages 1-3

# 5. 测试
& $py -m pytest tests -q
```

## 可入库清单与批增

`ingest_queue` 视图 = `status='classified'` 的记录（已排除 `rejected` 的非内容、`quarantined` 的隔离区件）。
导出的 CSV 就是"确认无重复、等着入库"的文件表，字段：

```
doc_hash, file_name, parent_dir, dir_path, ext, size_bytes, category, topic, collection, source_trust
```

```powershell
# 导出 / 统计 / 查重（新文件来了先按 hash 或文件名查一下）
& $py tools\ingest_queue.py --db $db --export "$build\ingest_list.csv"
& $py tools\ingest_queue.py --db $db --stats
& $py tools\ingest_queue.py --db $db --lookup 3afb55d83ce320e0

# 批增：把新文件丢进 31-收件箱，然后一条命令走完去重 → 分类 → 清单
& $py tools\pipeline.py --check                 # 先预览：只报会新增/重复多少，不写库
& $py tools\pipeline.py                        # 真跑
& $py tools\pipeline.py --root "<DOWNLOAD_DIR>\新面经"   # 也能直接处理别处的目录
```

判重口径（`intake` 的 `actions`）：`added` 新件 / `duplicate` 副本（原件仍在磁盘上，只刷"最后见到"，
**不改路径**）/ `moved`·`renamed` 原件已不在原处才改路径 / `quarantined` 隔离区只登记。

## 实测基线（2026-11-17 本机 i5-13400F / 31.9 GB）

| 项 | 结果 |
|---|---|
| 语料登记 | **1,501 行 = 1,351 可入库 + 87 隔离区 + 16 非内容 + 47 superseded（`.doc` 已转 docx）**，全量重扫 4.6 s |
| hash 速度 | 2,523 MB / 2.93 s ≈ 860 MB/s，单件 6.6 ms |
| 跨目录同内容 | 2 对（hash 抓到；`99-重复文件待删` 的 138 件里与库内字节相同的仅 1 件） |
| 分类分布 | 题库 559 / 真题 432 / 面经 235 / 专题笔记 125 / 非内容 16 / 未分类 0 |
| 可入库清单 | **1,351 行**（1,501 − 87 隔离 − 16 非内容 − 47 superseded） |
| topic 分布 | 未分类 117（题库类未命中领域，属诚实标注） |
| 本批导入（2026-11-17） | 收件箱 798 件 → `added` 771 / `duplicate` 27（27 件全是 `3-面经分享` 里与 `11.1-公司面经故事` 同内容的副本）；落库 面经 156 / 真题 392 / 题库 223；导入源原件已删（798 件 / 439 MB，逐件 hash 校验通过） |
| 第二批导入 `<DOC_ROOT>\docs`（2026-11-17） | 181 件扫描 → 111 件入收件箱（`面试题` 109 / `面经` 2）；**53 件与库内已有内容逐字节相同、58 件新内容（其中 19 件批内互重）→ 实际登记 37 行（题库 35 / 面经 2）**；原件累计删 110 件 / 875.8 MB（含修 `intake` 隔离副本规则后取回的 35 件）；剩 35 件全为排除件（书单 35、`.git` 29、mp4 1、图解包 5） |
| 第三批导入 `<DOC_ROOT>\面试准备学习资料`（2026-11-17） | 343 件扫描 → 211 件入收件箱（面试题 179 / 面经 32）；`added` 13 / `revived` 33 / `duplicate` 165（32 件面经全部与既有内容重复）；**登记 46 行，全为题库**；排除 121 件（电子书单 85、阿里技术白皮书 30、掘金计划 6 件）+ 非文档型 11 件（xmind 9、jpg 1、lnk 1）；清理重复 165 件（收件箱 244.7 MB + 源目录 244.7 MB，明细 `build/dedupe_deleted.csv`）、空目录 58 个；原件再删 65 件 / 1.28 GB（46 件本批 + 19 件书单里与库内重复的）、空目录 29 个，**源目录剩 113 件**（书单 69、阿里白皮书 30、xmind 9、掘金 3、非文档型 2） |
| .doc 转换 | 47/47 成功（Word 2007 的 `FileFormat=12` + **zip 魔数验收**；首轮用 `SaveAs2/16` 存出的是 OLE 顶着 `.docx` 后缀的假文件，已全部删除重转），原 `.doc` 记 `superseded`、新 `.docx` 由 intake 登记，清单总量不变（`.doc` 47 → `.docx` 47） |
| 同题频次（本批面试+笔试） | 614 份文档（4 份无文本层、0 异常）、3,289 个题目块、聚成 2,483 簇，其中 394 簇出现在 ≥2 份文档；明细 `build/question_freq.csv`（300 行）+ `docs/07-题目频次报告-2026-11-17.md` |
| 门禁（397 个 md） | pass 338 / non_content 41 / garbled 13 / low_cjk 5 |
| 门禁命中 | 复现 JavaGuide v1.0·V1.1·v2.0、Leetcode-Go、Nginx 手册、剑指Offer、大厂算法合集等"假成功" |
| OCR 引擎 | PaddleOCR-json v1.4.1，初始化 0.99 s（实测 0.72–0.80 s/页） |

## 与语料内既有管线的关系

不复制、不改写语料里已有的四个脚本，按职责分工：

| 环节 | 脚本 | 位置 |
|---|---|---|
| 登记 / 判重 | `intake.py` | 本仓库（新） |
| 分类 | `classify.py` | 本仓库（新） |
| 解析（pdf/docx/md → md 产物） | `convert.py` / `convert_one.py` | `…\21-面经数据库\tools\`（复用） |
| 质量门禁 | `gate.py` | 本仓库（新） |
| 拆题 | `parse_banks.py` | 语料内（复用，待接分类字段） |
| 建库索引 | `build_db.py` | 语料内（复用，需加 collection/source_trust） |

`registry.json` 从此是"一次扫描的快照"，**事实源是 `kb.sqlite` 的 `source_documents`**。

## 尚未实现（明确记录，不假装有）

- `classify --neighbor`（近邻判类）：需要文档向量索引，第②档归因；
- `cluster`（增量吸附）：canonical 表已建，算法未写；
- `reconcile`（四数对账）：用于清 `ok=423 vs 产物 391` 那 32 条旧账；
- `dispatch` / `extract`：v2 §6 里的拆题与分发环节。

详见 `docs/04-工具链SOP.md` 的"未实现清单"。
