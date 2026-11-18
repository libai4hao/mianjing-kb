# vibe-work

面经题库知识库工程的**代码与文档仓库**。语料与数据留在 `<CORPUS_ROOT>`（按方案不搬迁），
本仓库只放可版本化的部分：方案文档、元数据规则、管线脚本、测试。

> 仓库根为 `<REPO_ROOT>`（父目录 `vibe-work` 是共用工作区，另有 `公开课视频转文档`
> 等无关项目，不在本仓库内）。此前约定是"新项目统一放 `<REPO_BASE>`"，这里属于**有意识偏离**，
> 原因见 `docs/00-决策记录.md` D-09。

## 目录

```
docs/                     方案与决策（00 决策记录 ~ 17 审核修复；**06 部署方案 v2-R2 为当前口径**）
meta/
  schema/001~004_*.sql    schema：原文档表 / parent_dir+清单视图 / 源边界 / questions 表
  topic_rules.yaml        文件名 → topic（30 条，源自语料内 inventory.py）
  folder_rules.yaml       目录结构位 → category / source_trust；包装词；顶层主题目录；reject_patterns
  overrides.json          人工分类覆盖表（P1-5，最高优先级）
  exclusions.json         出题池排除表（P2-⑤：保留检索、不抽题）
tools/
  kb/                     公共库：config / db / hashing / textmetrics
  ── 入库管线 ──
  intake.py               登记 + sha256 判重（新增/重复/移动/改名/复活/隔离）
  classify.py             分类：规则判类 + 近邻投票（--neighbor 标题 Jaccard / --vec 语义向量）+ 人工覆盖
  gate.py                 文本质量门禁（三态 pass/review/fail，NFKC；--audit 产 dispatch 消费的报告；--sentinel 哨兵回归）
  ingest_queue.py         可入库清单：导出 CSV / 统计 / 查重
  pipeline.py             批增一条命令：去重 → 分类 → 清单（--check 只预览）
  ── 转换与 OCR ──
  doc_convert.py          .doc → .docx（Word COM + zip 魔数验收），原 .doc 标 superseded
  reconvert.py            同名覆盖重转（输出名带 doc_hash 后缀）
  backfill_convert.py     G1/G2 缺口补录驱动（圈缺口 → 复用语料侧 convert_one → OCR 清单）
  ocr_pages.py            PaddleOCR-json 页级 OCR（渲染 → OCR → 落盘，断点续跑）
  ocr_merge.py            OCR 定稿合并回正式 md（配图段保留，旧 md 备份）
  lazy_images.py          md 图片引用懒加载改写（<img loading="lazy">）
  ── 抽题与统计 ──
  chunker.py              切题器（v1 blocks / v2 items+probe+coverage，纯函数）
  extract.py              题块落 questions 表（幂等，孤儿清理）
  question_freq.py        同题频次：抽取 → 切题 → SimHash+Jaccard 聚簇 → 文档频率
  report.py               questions 表 → 近重复簇 → 频次报告（md+csv）
  canonical.py            题簇落库 canonical_questions / question_variants（全量唯一题登记簿）
  cluster.py              题级吸附 + 99 区可删 gate（strong/manual/none 三态）
  bookmarks.py            PDF 书签 → 合并版源边界落库（删 99 区前必跑）
  reconcile.py            convert_log ok 件 vs 实存 md 逐一对账
  dispatch.py             门禁 fail/review 件分流（六路由）
  review_cli.py           灰区人工复核入口（--resolve 改状态留痕）
  ── 检索与 LLM ──
  build_db.py             kb.sqlite → 面经库.db（FTS5 trigram 检索库）
  embed_questions.py      bge-small-zh-v1.5 向量化 → vec_questions
  search.py               混合检索（kw FTS + 向量 KNN + RRF；--format rag）
  llm_smoke.py            本地 LLM 冒烟对比（T1 RAG / T2 结构化抽取）
  llm_client.py           OpenAI 兼容客户端（provider 路由 / 重试退避 / JSON 宽松解析 / 脱敏）
  batch_label.py          LLM 批量打标（staging 暂存 / 预算封顶 / 断点续跑 / 抽查 / G6+merge）
  probe_provider.py       OpenAI 兼容 provider 探针（P1-P4 四项）
  ── 维护 ──
  refresh.py              检索库影子重建 + 向量回填 + G1-G5 门禁 + 原子切换（K1 编排）
  verify_ledger.py        G1-G7 核账门禁（任一硬门禁 FAIL 非 0 退出；FAIL 项产 backfill_plan.csv）
  purge_origin.py         删除已收入语料的原件（四重校验缺一不删）
scripts/
  补录-doc转docx.ps1       [DEPRECATED] 已由 doc_convert.py 取代，仅历史参考
tests/                    187 个用例（pytest 全绿；textmetrics/intake/classify/gate/切题/检索/分流/核账门禁/LLM 客户端/打标/高风险工具守护…）
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

> **历史快照**：下表是 2026-11-17 入库阶段的口径（登记 1,501 行、397 个 md、OCR v1.4.1）。
> 经过后续补录与两轮原子切换，当前生产为 **32,087 题 / 1,434 doc_contents / 18,642 向量**
> （topic 空串归零、G1-G7 全绿；详见 `docs/17-项目审核与修复报告-2026-11-17.md`）。
> 门禁终态以 `python tools/verify_ledger.py --docs` 输出为准，勿把下表当成现状。

| 项 | 结果 |
|---|---|
| 语料登记 | **1,501 行 = 1,347 可入库 + 37 隔离区（99 区剩 37 件待人工）+ 20 非内容 + 97 superseded（`.doc` 已转 docx）**，全量重扫 4.6 s |
| hash 速度 | 2,523 MB / 2.93 s ≈ 860 MB/s，单件 6.6 ms |
| 跨目录同内容 | 2 对（hash 抓到；`99-重复文件待删` 的 138 件里与库内字节相同的仅 1 件） |
| 分类分布 | 题库 558 / 真题 429 / 面经 235 / 专题笔记 125 / 非内容 20（含 4 份空 docx）/ 未分类 0 |
| 可入库清单 | **1,347 行**（1,501 − 37 隔离 − 20 非内容 − 97 superseded） |
| topic 分布 | 未分类 114（P0-3 近邻投票后：3 件自动落库 neighbor，114 件待人工清单 `build/topic_neighbor_review.csv`，附提案与邻居证据） |
| 本批导入（2026-11-17） | 收件箱 798 件 → `added` 771 / `duplicate` 27（27 件全是 `3-面经分享` 里与 `11.1-公司面经故事` 同内容的副本）；落库 面经 156 / 真题 392 / 题库 223；导入源原件已删（798 件 / 439 MB，逐件 hash 校验通过） |
| 第二批导入 `<DOC_ROOT>\docs`（2026-11-17） | 181 件扫描 → 111 件入收件箱（`面试题` 109 / `面经` 2）；**53 件与库内已有内容逐字节相同、58 件新内容（其中 19 件批内互重）→ 实际登记 37 行（题库 35 / 面经 2）**；原件累计删 110 件 / 875.8 MB（含修 `intake` 隔离副本规则后取回的 35 件）；剩 35 件全为排除件（书单 35、`.git` 29、mp4 1、图解包 5） |
| 第三批导入 `<DOC_ROOT>\面试准备学习资料`（2026-11-17） | 343 件扫描 → 211 件入收件箱（面试题 179 / 面经 32）；`added` 13 / `revived` 33 / `duplicate` 165（32 件面经全部与既有内容重复）；**登记 46 行，全为题库**；排除 121 件（电子书单 85、阿里技术白皮书 30、掘金计划 6 件）+ 非文档型 11 件（xmind 9、jpg 1、lnk 1）；清理重复 165 件（收件箱 244.7 MB + 源目录 244.7 MB，明细 `build/dedupe_deleted.csv`）、空目录 58 个；原件再删 65 件 / 1.28 GB（46 件本批 + 19 件书单里与库内重复的）、空目录 29 个，**源目录剩 113 件**（书单 69、阿里白皮书 30、xmind 9、掘金 3、非文档型 2） |
| .doc 转换 | 47/47 成功（Word 2007 的 `FileFormat=12` + **zip 魔数验收**；首轮用 `SaveAs2/16` 存出的是 OLE 顶着 `.docx` 后缀的假文件，已全部删除重转），原 `.doc` 记 `superseded`、新 `.docx` 由 intake 登记，清单总量不变（`.doc` 47 → `.docx` 47） |
| 同题频次（本批面试+笔试） | 614 份文档（4 份无文本层、0 异常）、3,289 个题目块、聚成 2,483 簇，其中 394 簇出现在 ≥2 份文档；明细 `build/question_freq.csv`（300 行）+ `docs/07-题目频次报告-2026-11-17.md` |
| 门禁（397 个 md） | pass 338 / non_content 41 / garbled 13 / low_cjk 5 |
| 门禁命中 | 复现 JavaGuide v1.0·V1.1·v2.0、Leetcode-Go、Nginx 手册、剑指Offer、大厂算法合集等"假成功" |
| OCR 引擎 | PaddleOCR-json v1.4.1，初始化 0.99 s（实测 0.72–0.80 s/页） |

## 与语料内既有管线的关系

不复制、不改写语料里已有的脚本，按职责分工：

| 环节 | 脚本 | 位置 |
|---|---|---|
| 登记 / 判重 | `intake.py` | 本仓库（新） |
| 分类 | `classify.py` | 本仓库（新） |
| 解析（pdf/docx/md → md 产物） | `convert.py` / `convert_one.py` | `…\21-面经数据库\tools\`（复用） |
| 质量门禁 | `gate.py` | 本仓库（三态口径；语料侧同名脚本已降级为历史参考，见 docs/17） |
| 拆题 | `chunker.py` / `extract.py` | 本仓库（新，v2 切题器） |
| 建库索引 | `build_db.py` | 本仓库（新，FTS5 trigram 检索库） |

`registry.json` 从此是"一次扫描的快照"，**事实源是 `kb.sqlite` 的 `source_documents`**。

## 尚未实现（明确记录，不假装有）

- ~~`verify_ledger.py`（G1-G7 统一核账门禁）~~ **已落地**：G1-G5 终检全 PASS，生产 面经库.db
  已原子切换（1,434 doc_contents / 18,642 题 / 18,642 向量 / topic 空串归零）；偏差见 docs/17；
- ~~canonical 落库（cluster 算法 → 存储层）~~ **已落地**：8,931 簇 / 18,642 变体，
  增量吸附待题量到 1e5 再做（docstring 简化声明）；
- ~~向量近邻②档（classify --vec）~~ **已落地**：当前文档级未分类 0 件，能力待新批次导入；
- ~~`refresh.py`（K1 影子编排）~~ **已落地**：已执行首次生产原子切换；
- ~~chunker v2 判题信号增强~~ **已落地（2026-11-17）**：独立问句行升级为块起点（与 R2' 同口径）
  + `问题N` 题号形态（面向字节/连环炮系列主流）+ 整行粗体剥壳 + 配图标题落 leftovers
  + RE_LIST_ITEM 负向前瞻（`**` 不再误判列表项）。全量重切 **18,642 → 32,087 题（+13,445）**，
  skip_log 诚实清理（773→430 条），G1 同名族判定修复 23 件错位误报；
  终检 G1-G5 全 PASS，生产库二次原子切换（13,716 canonical 簇）；
- ~~G6 打标质量门禁~~ **已落地（2026-11-17）**：`verify_ledger` G6（schema 合格率 ≥0.95 硬门 +
  topic 漂移 >15% WARN）+ `llm_client.py`（provider 路由/重试退避/JSON 宽松解析/脱敏）+
  `batch_label.py`（staging 暂存/预算封顶/断点续跑/--sample 抽查/G6 复核 merge）+
  006 迁移（questions 题级 topic 列，打标事实源落点）。**key 一配置就可以用**：
  `[Environment]::SetEnvironmentVariable('LLM_KEY','sk-xxx','User')` 后
  `python tools\batch_label.py --check` → 打标 → `--sample 50` 人工抽查 → `--merge`
  → `python tools\refresh.py --reembed`（检索库 topic 是快照，merge 只写 kb.sqlite）。
  local 路径（ollama qwen3-4b）已端到端实测（3 题真打标→G6→merge 落库）；
- **向量增量（K2）**：当前全量重嵌约 9 分钟，暂无增量需求。

## 仍待消化（数据债，非代码缺口）

- ~~G1-G3 数据债务~~ **已清零（2026-11-17）**：487 篇无题 → 转换 528 + OCR 补录 12 件全落盘，
  131 篇有 md 未入库 → 重建并入，5,192 topic 空串 → 继承链归零；终检 G1-G5 全 PASS。

详见 `docs/04-工具链SOP.md` 与 `docs/15-检验门禁与线上打标方案-2026-11-17.md`。
