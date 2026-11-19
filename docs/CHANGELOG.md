# 工程变更日志（CHANGELOG）

> 状态：现行 · 最后更新：2026-11-19 · 适用版本：v2-R3
>
> 只记**工程变更**（代码、门禁、依赖、文档结构），不记数据快照——题数、库大小会随重建漂移，
> 一律以 `docs/29-数据基线与统计口径-2026-11-19.md` 为准。历史里程碑按日期倒序。
> 2026-11-16 之前的记录见 `docs/archive/` 与 git 历史，本表从外部 review 落地开始。

## 2026-11-19

- **question_freq 归一器 v2 + 迁移工具**：NFKC 后做代码符号语义映射（`C++`/`C#`/`&&`/`&`/`i++`/`++i`），
  CJK 清洗范围扩到扩展 A/兼容区；`config.NORMALIZER_VERSION="nfkc-clean-v2"`。新增
  `mianjing_kb/migrate_norm.py`，dry-run 出 old→new 快照，`--apply` 同一事务更新 `questions` 与
  `label_facts`，同文档新 hash 碰撞按答案最长保留；生产 dry-run 2,299/20,010 条 hash 变化、0 删除。
- **近重复聚类替换 LSH**：`question_freq.cluster` 与 `canonical.attach_incremental` 改用 2-gram 倒排 +
  前缀过滤 + Jaccard 精确判定，去掉 SimHash 16bit 分带/汉明前置过滤；`--dist` 仅保留兼容。
- **抽取器加固**：PDF `get_text(sort=True)`；DOCX 保留 `w:br`/`w:tab` 与 `w:numPr` 自动编号；Word COM
  禁宏/关提示/只读打开参数/失败重试并抛异常；`extract.MIN_NORM=4`；`split_qa` 标 DEPRECATED。
  question_freq CLI 排序新增 `n_collections`，与 canonical witness 的 distinct collection 口径统一。
- **label_facts 加固（主键不变）**：保留 `q_norm_hash`，不迁 `q_key`；017 迁移补唯一索引、
  `normalizer_version`、`q_norm_sample`、`source/labeled_at/confidence`，`merge` 写归一版本与题干快照；
  同一归一题干跨文档共享标签，`extract.store` 重挂逻辑不动。按决策纪要未建 `label_overrides`：
  生产 `label_facts` 0 行、重复 0、跨 topic 冲突 0；冲突量上来再单独建表。
- **chunker v2 召回修复**：`extract.text_of` 保留 `## 2.`/`问题N`/`一、` 等结构性标题、只对非题目型
  PDF 断句标题去 `#`；`_match_question` 先剥整行粗体；推广过滤改成强特征直剔/弱词仅在不像题目时剔；
  水印候选排除 `答案：/优点：` 等冒号标签；页码/OCR 分页标记不消费 prev_blank，标题/文档头视为块边界；
  答案内冒号小节与不足 6 个归一化字符的碎片不另起题；`chunk_v2` 用 `split_long=False` 保持
  完整块与 a_segs，长答案 span 不再错位；`_HAN` 扩到 1–99，`问题二十一` 不再得 qid=0。
- **extract 数据安全修复**：`purge_orphans` 仅基于完整题池键集合；`--limit`/增量/报错超阈值
  时跳过清理；每篇文档用 `SAVEPOINT extract_one` 独立提交，单篇失败不再中断整批；空结果且
  已有旧题时默认保留并记 `empty_preserved`，新增 `--allow-empty-overwrite`。
  `doc_pool` 的源 rel 改由 `source_documents.dir_path + file_name` 生成，排除规则同时校验
  源 rel 与 md rel；非内容池源留下的 md 孤儿不再重新进池。文本读取统一走 `kb/textio.py`
  （`utf-8-sig → gb18030 → utf-8 replace`）并做 NFKC；标题剥离逐行处理并跳过代码围栏。
  `MIN_CHARS` 与 `gate.TH_MIN` 统一为 `config.TEXT_MIN_CHARS=100`；`--stats` 只读且
  `freq_top10` 显式排序；`--plan/--dry-run` 不再写 `md_identity_report.json`。
- **canonical variant_id 改稳定键**：`variant_id = sha256("variant:" + q_key)[:16]`，seq 移位
  不再改变变体身份；新增 `normalize_variant_ids` / `prune_orphan_variants`，增量 attach 与
  `--apply-candidates` 先清理历史 `(doc_hash, seq)` ID 与 q_key 已消失的变体；孤儿 md 的
  `doc_meta` 统一归到 `md-orphan`，避免按 doc_hash 虚高见证数。
- **T30 门禁结论前置到抽题**：`gate --audit` 把三态报告映射为 `gate_results` 四态
  （`pass/low_cjk/garbled/non_content`）并同步 `source_documents.gate_result`；`extract` 在
  `doc_pool` 后按 `doc_hash` 过滤，只抽 `pass`，`skipped_gate` 按四态计数，新增
  `--require-gate`（无台账也跳过）。全量 `purge_orphans` 仍用过滤前身份池，避免门禁跳过误删旧题。
- **gate.py 静默误判修复**：报告先落盘再写库；`--strict` 在真覆盖或异常率超限时返回 1；
  `--audit`/`--md-dir` 互斥；哨兵全 MISS 触发防护失败；`run_audit` 统一走 `resolve_md`
  （按 `doc_hash` 前 8 位选变体，通用同名文件只允许同主题目录或含 doc_hash 才绑定）；
  `collisions_of` 过滤缺失产物、按 rel 去重、改用行内 `doc_hash`；`read_text` 改为
  `utf-8-sig → gb18030 → replace` 并记录实际编码；长度/CJK/异常字符按可见字符统计，
  去掉分页标记/代码围栏/Markdown 噪声；白名单放行 emoji/希腊/假名/CJK 扩展，单列
  `U+25A0–25FF` 方框乱码；`fixed` 改为实际 NFKC 改写数；修 `md_path_for` 带点 stem 截断、
  `md_index` 大小写、`gate_results` stale 清理、`pix_fp` 增加 `page_count`。
- **文档同步**：`docs/19` 增 T30 门禁前置节点与四态映射；`docs/33` 更新 gate/extract
  CLI、模块职责、`gate_results`/`source_documents` 数据表与已知边界；`docs/22`、`docs/32`、
  `docs/项目审核报告/待办清单-2026-11-19.md` 同步 T30 状态。
- **canonical 数据安全与增量修复**：增量候选只更新 `pending` 行，`approved/rejected` 不再被
  重置；全量重建保留 `merge_candidates`，`canonical_id` 按成员重叠粘性绑定并回写旧 ID；
  增量默认阈值对齐全量 `--jac 0.7`（0.65–0.7 候选），新增 `--candidates/--approve/--reject/
  --apply-candidates` 消费链路；variant_id 冲突显式替换并清理旧 dependent；修正
  created/attached 计数、单例判定、topic 多数票与 collection 归一化；gram/simhash 预计算 +
  LSH 前置过滤；dry-run/stats 改只读，增量 dry-run 在内存副本上计算，不再申请写锁。
- **intake 判重顺序修复**：目录/文件显式排序，正式区整体优先于 `99-重复文件待删`，同一
  SHA256 不再由 `os.walk` 首次命中决定身份；新增隔离区先扫描的回归测试。
- **pipeline 失败传播**：intake/classify/ingest_queue 任一阶段失败即停并返回 1，不再把
  子进程失败吞成成功。
- **CI 与 CLI 编码收口**：GitHub Actions 的 checkout/setup-python 升到 Node 24 版本
  （`actions/checkout@v5` / `actions/setup-python@v6`）；Windows job 强制 `PYTHONUTF8=1` /
  `PYTHONIOENCODING=utf-8`。新增 `kb/stdio.py`，所有 CLI 入口在打印前把非 UTF-8 的
  stdout/stderr 重配为 UTF-8，修复重定向输出时中文 JSON 触发 `UnicodeEncodeError`；
  `demo` 子进程仅设 `PYTHONIOENCODING`，不再开启 UTF-8 mode。`eval_cluster` 在未配置
  `BUILD_DIR` 且未传 `--out-md` 时明确 exit 2，不再对占位路径 `mkdir` 抛 WinError。
- **代码地图与流程回写**：新增 `docs/33-代码地图与维护索引-2026-11-19.md`（分层 / 36 个 CLI 命令 /
  模块职责 / 数据表 / 测试地图 / 改动决策表）；`docs/19` 同步到 T29/T31/T22 代码现状（题行 20,010、
  检索库 17:55、G1–G8、`refresh` 题集指纹、`vec_meta`/`question_tags`、`kb status`/`manifest`），
  T22 明确为“代码已下沉、生产未打标”，不再写成未实现；`docs/29` 的 `schema_version`
  由 15 条改为实际 16 条（001–016）。
- **T31 `kb status`**（`fde9542`）：只读汇总源状态、产物/待抽题、题与标签覆盖、检索库向量/标签行、
  门禁报告指纹是否过期、99 区结果、写锁与最近批次；`kb status --json` 可脚本消费。门禁报告
  与当前 kb/面经库大小+mtime 不一致时标 `STALE`，不重跑门禁。
- **T1+T22 检索过滤下沉**（`e962b94`）：`vec_questions` 增 `topic/qtype/difficulty` vec0 metadata，
  `--topic/--qtype/--difficulty` 在 KNN 阶段过滤；检索库 `questions` 下沉 `difficulty/tags_json/
  position/quality`，新增 `question_tags(qid,tag,tag_norm)` 索引；`search` 增
  `--difficulty/--position/--quality/--tag`（多 tag 为 AND）。G4 校验 vec 元数据逐行一致，
  旧裸表 FAIL 要求 `--reembed`。
- **T29 稳定键与增量**：迁移 `016_stable_question_keys` 增 `questions.q_key` 唯一索引
  （`(doc_hash,q_norm_hash)` 重复题按答案最长保留，生产 20,984 → 20,010），`question_variants`
  增 `q_key`，新增 `canonical_dependents` / `merge_candidates`；`extract` 同步去重。
  `canonical --incremental` 实现 0.92 吸附 / 0.85–0.92 候选 / 新簇三路，只重算受影响 witness；
  （2026-11-19 后续已统一为全量 `--jac 0.7` + 0.65–0.7 候选，见顶部“canonical 数据安全与增量修复”）
  `manifests.py` 提供 `kb manifest --list / --rollback [--apply]`。`refresh` 增题集指纹：
  自动拷贝前校验 `vec_meta.question_set_hash`，结构/指纹不匹配自动重嵌入，`--copy-vec` 显式失败即止。
- **待办收口（T6 / 遗留两项）**：`skip_log` 明确留在 `meta/`、个人数据永不入库；G1 对
  `duplicate_of` 增加目标存在性回查；`cluster --lsh-recall-sample N` 抽样暴力比对测候选召回
  （生产抽 30 条、227 真匹配，召回 1.0）。
- **自审复核修复（c09f6cc 后）**：`--no-cache` 不再清空缓存；`--limit` 在全量结果不存在时也不占用
  `cluster_result.json`；抽取任一件失败时结果标 `complete=false/failed=true` 并另写
  `cluster_result_incomplete.json`；G4 的 `questions_fts` 缺失/损坏改为 FAIL 而非抛栈；
  G1 精确映射按目录+base_stem 收敛 `__hash8` 后缀；skip 模板补齐原因枚举并注明个人数据永不入库。
- **cluster 99 区 gate 收紧**：文件级 `strong` 需每条 99 题直接匹配池题（≥0.92，不用 single-linkage
  传递）、池样本账本状态合法、单向答案覆盖、匹配占比 ≥90% 且答案/文本覆盖达标；其余 `manual`。
  缓存 key 带版本、失败不缓存、Word 锁文件跳过、`--limit` 结果标 `complete=false` 并写独立文件。
- **verify_ledger 复核修复**：G5 空 `src_path` 不再被 `Path("")` 判成存在；G4 向量表在但扩展不可加载
  改 FAIL；G6 schema FAIL 归硬门；skip_log 原因白名单；G1 优先精确源→md 映射；G6b 样本下限 50、
  weak topic 最少 5 条；报告绑定 db 指纹；backfill_plan 只收 G1/G2。
- **检索库重建收口**（`e22108b`）：`refresh._counts()` 查向量前加载 `sqlite-vec`；
  失败 summary 增加 `gate_fail_samples`；`refresh --reembed` 在影子库上过 G1–G8 后原子切换
  `面经库.db`，题/向量同数。
- **全量重跑与基线**（`d443669`）：`extract` 单身份重抽、`canonical` 全量重聚簇，`docs/29` 整体重写，
  新增只读 `baseline_stats.py` 复算基线。
- **P1-6 文档口径收口**（`9f28957`）：R1/R2/R3 全部归档、`docs/32` 成唯一口径索引；
  `kb` / `online` 双写锁；`build_db` 默认拒绝直跑；新增 `rehearse_migrate.py` 迁移预演。
- **Q-2 / Q-3**（`8463267`）：见证数改按 distinct `collection`；`document_products` 产物台账、
  `processing/processed` 状态、`extract --incremental`。
- **T27**（`413b3e2`）：题簇成对评测 P/R/F1、阈值扫描、题长/答案/coverage 质量指标。
- **P0-1**（`082e244`）：题池只出源身份，镜像题行清零，G8 专查双份残留。
- **T16**（`ba4b97b`）：Word 转换改为逐件子进程 + 硬超时，`kb/proc.py` 与 D-24 四层契约。
- **版本入口收敛**（`830af77`）：R1/R2 归档，`19`（流程）/`29`（数字）唯一化。
- **LLM 密钥脱敏**：`llm_client.scrub_secret()` 在 HTTPError / URLError / 重试汇总出口统一清洗
  `LLM_KEY`、`sk-*` 与 Bearer 头；新增回归用例。
- **purge_origin 哈希校验**：短指纹只用于定位行，是否“已收入”必须有**完整 sha256** 相等；
  短指纹撞车时拒绝删除。
- **工程化补齐**：`pyproject.toml`（`kb` console script、可选 `dev` 依赖）、GitHub Actions
  `windows-latest` × Python 3.11/3.12（ruff + pytest + 隐私门禁 + demo）、`requirements.txt`
  `pywin32` 条件依赖；删除易漂移的 README 统计数字，改由 `verify_ledger --docs`（G7）校验。

## 2026-11-17

- **T11–T19 落地**（`e027d7c`）：分块提交、事件表、状态常量、迁移事务、写锁、转换器入仓、G5 历史校验。
- **包名 `tools` → `mianjing_kb`**（`99791b3`）：统一包入口，脚本不再依赖仓库根目录的相对导入。
- **评审修复**（`6a7fb68`）：wlock 释放与半写宽限、G5 裸目标歧义、doc_convert 逐件提交、
  intake `*_from` 记旧路径、测试导入包化。

## 2026-11-16

- 首轮语料整理与方案定稿（R1），原始记录见 `docs/整理报告-2026-11-16.md`。
