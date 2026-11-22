# 工程变更日志（CHANGELOG）

> 状态：现行 · 最后更新：2026-11-22 · 适用版本：v2-R3
>
> 只记**工程变更**（代码、门禁、依赖、文档结构），不记数据快照——题数、库大小会随重建漂移，
> 一律以 `docs/29-数据基线与统计口径-2026-11-19.md` 为准。历史里程碑按日期倒序。
> 2026-11-16 之前的记录见 `docs/archive/` 与 git 历史，本表从外部 review 落地开始。

## 2026-11-22

- **原始 PDF/DOCX 全链路小规模测试 + 检索层 F1 修复**：11 件原始文件（8 pdf + 3 docx，55 页）在独立测试库
  （`build/test-run-2026-11-22/`）从头跑 intake→classify→convert→gate→extract→canonical→build_db→embed→verify→refresh：
  11/11 转换、219 题零空答案、G1–G5/G8 全 PASS，生产库指纹前后一致。报告 `docs/41-原始PDF-DOCX全链路小规模测试-2026-11-22.md`
  记录 5 项发现；其中 F1（`bge-small-zh` 把 ThreadLocal/Kafka/JVM/Redis 全映射成 `[UNK]`，余弦 1.0，
  叠加整句短语匹配失效导致 `X 是什么` 首位错）当日修复：`search.py` 新增 `latin_terms` / `entity_route_terms`
  （仅单实体且中文 ≤6 字的短问）、`keyword_search(allow_entity_route=)` 词项回退、单实体短问的向量候选聚焦、
  `_row_has_term` 去空白归一、并列破除 `entity_tiebreak`。50 条金标 recall@5 0.6932→0.7159、MRR 0.6508→0.6521；
  期间否决了两版会回退的方案（+0.0041 加权 ≈15 个 rank 槽、无闸门词项 OR 灌噪声）。新增 6 条用例，全量 pytest 740 collected 全绿。
  未修（本轮）：F2（`source_path` 恒空）、F3（`refresh` 不落 `verify_report` → `status` 恒 STALE）、F4/F5（题干 Markdown 残留、md 命名两套并存）——F2/F3 见下一条同日修复。

- **F2/F3 修复（同一轮测试的后两项发现）**：F2——`search.py` 的 `source_path` 原先只按
  `doc_contents.mirror_hash=q.doc_hash` 连接，正常文档 0/219 命中，恒空；改成 `doc_hash` 优先、
  `mirror_hash` 兜底（孤儿 md 题）的标量子查询，并用“题库/ 优先 + 最短”只挑一条 md，
  避免生产 48 组“同一 doc_hash 多份 md”把结果行放大。新增 2 条用例。F3——`refresh.py` 切换成功且
  `_mark_indexed` 回写之后，新增 `_stamp_verify_report()` 对线上库重跑 G1–G5/G8 并落
  `verify_report.json/.md` + `backfill_plan.csv`（报告必须晚于 `indexed_at`，否则又盖旧指纹；写失败只告警不回滚）。
  测试环境实测 `status` 由 `STALE` 转 `PASS verdict=PASS`；新增 1 条用例。详见 docs/41 §10–§11。

- **canonical 正向合并审计（补“该合的能合”这一半）**：新增只读审计 `python -m mianjing_kb.canonical_audit`
  （`mianjing_kb/canonical_audit.py`），按 canonical 的 Jaccard 契约精确枚举题对，量化四件事：契约召回
  （≥0.7 是否同簇）、链式传递（同簇但 <0.7 的对）、改写证据（同簇且 q_norm 不同）、候选带 [0.65,0.7) 计数。
  生产 20010 题实测：20496/20496 契约对同簇、跨簇 0 → PASS；同簇 20879 对里 4412 对文本不同（真改写合并）、
  383 对低于阈值（链式）、候选带 871 对只拆不合。同时新增 `tests/test_canonical.py` 两条正向/边界用例
  （0.812 改写跨文档合簇、0.692 语序调换按契约拆开）与 `tests/test_canonical_audit.py` 3 条。
  测试库注入改写副本探针：219→224 题、220 簇，3/3 改写合到原簇，0.692 对照未合；但暴露反向风险
  （`什么是springmvc` 并入 `什么是spring?`，0.727）→ 记为 F6（短题 Jaccard 过合），修复方案见 docs/41 §12.7，未实施。

- **F4/F5 修复**：F4——新增 `mianjing_kb/kb/qtext.py::clean_question_text`，在 `extract` 落库前解 `**…**`、
  剥行首题号（`**1** 、`/`10.`/`第5题：`/`（3）`）、收空格；题干含围栏/反引号/换行/花括号/分号或
  `int**` 指针写法时整段不动，`a_text` 一字不改；`q_norm`/`q_key` 仍按原串，身份不漂移。
  测试库重抽后 `q_text` 含 `**` 50→0、`a_text` 44 不变、题数 219 不变，`refresh --reembed` 全链 PASS。
  F5——`gate.py` 新增 `_is_hash_variant_of`，同目录同 stem 且 hash8 对得上 `doc_hash` 的
  `__/_<hash8>` 产物不再记 drift；本轮测试环境 `gate --audit` 的 drift 11→0，真正指向别的目录仍报 drift。
  新增 5 条 qtext 用例 + 2 条 gate 用例；存量 md 命名统一（997/28/409 三套）未迁移，单列。详见 docs/41 §13。

- **F4/F5 生产落地（线上生效）**：全量 `extract` 被实测否决——生产 25 篇抽样重切后 16 篇题集不同（合计 227→835 题），
  等于全库重切 + 重建标签/canonical/向量。改为新增 `mianjing_kb/clean_qtext.py`：只对存量 `questions.q_text`
  过 `clean_question_text`，`q_norm`/`q_norm_hash`/`q_key`/`a_text` 一律不动（默认 dry-run，`--apply` 才写，走 kb 写锁），
  并加 3 条用例。清洗规则同步收窄：空格整理只在真的解掉 `**`/剥掉题号时做，生产 dry-run 由 1552 条降到 760 条
  （排除 TOC 点线与乱码行的顺带改写）。生产执行：`kb.sqlite` 与 `面经库.db` 先备份到
  `build\backup-2026-11-22-f4\`；`clean_qtext --apply` 改 760 条后按 qid 定点同步线上库的 `questions.q_text`
  + 该行 `questions_fts`。结果：题 20010 / 向量 20010 不变、`q_text` 含 `**` 434→28（残留均为单边 `**`，
  属成对才解的预期保护）、FTS 20010 行且 `MATCH 'redis'` 正常。`refresh` 无法用于生产重建：`build_db` 的排除表
  会把 38 篇退役源对应的 94 条历史题挡在发布池外，导致向量对齐 ABORT，故本轮不重建、只定点洗显示。
  F5 生产 `gate --audit` drift **0**（collisions 14、missing 24 为既有）；audit 会写 `gate_results` 使 status 短暂
  STALE，按 F3 顺序补跑 `_stamp_verify_report` 后 `status` **PASS**（`at=2026-11-22T20:34:32`，G1–G5/G8 全 PASS，15.4 s）。
  详见 docs/41 §14。

- **P0 退役两篇 rejected 学习路线源的 94 题（生产 refresh 恢复可用）**：排查 `refresh` ABORT 的真因——线上多出的
  94 题来自两篇 `rejected` 源 PDF（`Java 后端学习路线（4w+字，超全面）.pdf` / `黑夜模式-…pdf`，都属“非内容”），
  `extract._orphan_owned_by_retired_source` 已按 stem 正确挡掉它们的 md，是线上库留着这批题。新增
  `mianjing_kb/retire_questions.py`（按 doc_hash 删 questions/FTS/向量/canonical 依赖并清空簇，默认 dry-run，
  写锁按库自动选 kb/online）+ 3 条用例。生产执行：kb.sqlite 删 94 题/94 dependents/94 variants/43 空簇；
  检索库删 94 题+94 FTS 行并清 94 条孤立向量；两篇 md 登记 `meta/skip_log.json` 的 `no_question`（G2 ③ 要求）；
  `refresh` 走 copy-vec 一次通过 → **19916 题 / 19916 向量**，G1–G5/G8 全 PASS（16.2 s 门禁、总 370.7 s，
  快照 `面经库-20261122-210830.db`），`status` PASS `at=2026-11-22T21:08:49`，待打标 334→240。详见 docs/41 §15。

- **原始文档小规模测试与再修 3 项**：用 5 篇 raw_docs 实跑 `chunk_v2` + 异常检测，从 100+ 异常中筛出 3 个可安全修复的真 bug（答案行以方法调用 `start()` 结尾被误判选择题空括号、`答：…` 回答行成题、`聊了聊项目；` 分号叙述行成题），TDD 修复：`_is_qline` 空括号规则排除方法调用 + 结论排除前缀补 `答：/回答：/解析：/解答：`、F5c 叙述行门补 `；/;`；028 异常 2→0、074 异常 4→3；新增 3 条回归 fixture。报告见 `docs/原始文档小规模测试报告-2026-11-22.md`。chunker 套件 141 passed，全量 pytest 728 passed / 7 skipped / 1 failed（基线无关）。
- **切题逻辑金标驱动修复（F1–F7，7 提交 `2a9a58d..21dc1d5`）**：依据人工金标 100 条样本中
  5 篇文档 20 条的复核，修复 8 类结构性错误中的 8 条（TOC 条目被当题、目录幽灵答案与真身
  并存、PDF 粘连层级节标题导致答案 bleed、面试官嵌入问句丢失 + 短标签碎片偷答案、
  编号标题无问句语义门槛把技能罗列/叙述行/列表项当题）。新增 `_toc_density`、`_toc_ghost_answer`、
  `_heading_questionish`、`hier_section` 硬边界、`last_qid` 单调推进、`RE_QUESTION_INTENT`
  补"说一下"；`_toc_mask` 窗口扫描改 H12 索引 + bisect（O(n·5000)→O(n+log n)）。
  新增 `tests/test_chunker_gold_regression.py`（每任务 TDD，先红后绿）。
  公共 API 与输出 schema 不变。20 条金标对账 8 条结构性错误全部不再复现；075 文档
  幽灵/假题 276→225；coverage 0.992→0.828 是**诚实下降**——旧值把 bleed 内容计入
  covered 虚高，层级节标题按标注口径（E_SECTION_HEADER）归 leftovers，非回归。
  全量 pytest 725 passed / 7 skipped / 1 failed（唯一失败为基线既有、与 chunker 无关的
  `test_doc_convert_isolation.py::test_hanging_file_is_recorded_and_batch_continues`）。
  详见 `docs/superpowers/plans/2026-11-22-chunker-gold-fixes.md`。

## 2026-11-20

- **切题准确率重构（D1–D3）**：`chunker` 新增题干续行回接（悬空题干 + 问句形态短续行）、
  `chunk_v2` 代码碎片出口终检（编号代码行不独立成题，内容并入父答案/leftovers）、
  大题说明行不产题 + 强提问语义编号子题不被列表状态吞掉；新增 `tests/test_chunker_accuracy.py`。
  公共 API 签名与输出 schema 不变；25 个基线语料快照零差异；测试库 A/B 验证 13 删 7 增均为预期修复。
- **review2 结构修复选择性移植（S1–S5）**：复核 `review2` 累积补丁（基线为原始上传 ZIP，
  与本仓库 lineage 不同）后，仅移植仓库缺失的 5 类结构行为：问句/任务语义优先于题号连续性、
  TOC NFKC 兼容字符与无页码变体 + 局部大纲首现屏蔽、章节标题硬边界与 coverage 扣除、
  行内粘连层级题号虚拟行拆分（span 精确）、强证据题干续行收回。引入
  `tests/test_chunker_structure_regression.py`（14 例）、`tests/test_question_quality.py`（7 例）
  与 `tests/fixtures/dev_ocr_numbered_code.md`。补丁中打标质量门禁（labelspec/verify_ledger 内联版）、
  llm_client/pilot5000/quality_rules 旧版改动**不移植**——仓库已有更新或等价实现
  （`label_quality.py`、URL 精洗、RPM 节流、行首问词守卫）。`cluster.CACHE_VERSION` 升 `v2-2026-11-20-r2`。
- **review2 移植精度回收（R5）+ 80 文档独立重放**：用 review2 附带的 `cut1000-doc-replay-stats.csv`
  对 80 篇生产 md 做只读重放（源件长度 80/80 与 zip 期一致，无漂移），发现 S1 的
  `explicit_question_heading` 把"父题下重启编号的答案要点"（`## 1、只有一个文件 dump.rdb…`
  紧跟 `## 38、…？`）误判为新题。新增 `answer_subpoint_restart` 判别（`qid<=last_qid` 且无
  问句/任务语义 → 答案小节），首个题头/顺延编号仍按 Markdown 编号题头处理。Redis 42 道
  72→50、JavaGuide 夜间模式 577→389、Java 并发合集 464→432，回收的误判题回到父答案。
  新增 `test_numbered_answer_subpoints_under_parent_are_not_questions`。`CACHE_VERSION` 升
  `v2-2026-11-20-r3`。残留：单条 OCR 粘连行（问 4 粘在问 3 答案尾，无 `## 4、` 题头）仍产
  少量碎片，属源件缺陷非系统性回归；`cut1000-regression-results.csv` 的 qid 属 zip 期 DB
  代际，与现生产库 qid 不互通，样本级状态无法逐条复核（已按文档级口径独立验证）。
- **切题样本生成工具留挡 + 新切题器口径样本流程**：新增 `scripts/make_cut1000_samples.py`
  （只读抽样：按 `q_norm_hash` 去重的切题样本 + 对应原始 Markdown 全文 + 逐题原文片段/前后文；
  CSV 公式注入防护、sha256 清单、manifest/metrics、无第三方依赖自检脚本口径）。打标审核样本
  沿用既有 `mianjing_kb/label_samples.py`（pilot100 schema）。样本数据属内部资料，落
  `D:\pilot\5000\build\samples`，不入仓库。新切题器口径样本流程：复制基线库到隔离副本 →
  `extract --allow-empty-overwrite` 全量重抽（清空新切题器判 0 题的旧碎片文档，保证纯新口径）→
  以重抽副本为源抽样；全程不写生产库与基线库。

## 2026-11-19

- **D-27 生产迁移完成**：生产 `kb.sqlite` 显式 apply 017（`017_label_facts_hardening`），
  再 `migrate_norm --apply`（2,299/20,010 题键变化、0 删除、11,994 映射），全量重建 canonical
  （11,074 簇 / 20,010 variants / 孤儿 0），最后 `refresh --reembed`（20,010 向量，G1–G8 PASS、
  G9 WARN）原子切换 `面经库.db`。迁移前双库备份与 old→new 快照保留在 `build/backup/`。
- **阶段 A 生产安全落地**：新增 `kb preflight` 只读基线核对（`schema_version`/迁移/完整性/关键计数）；
  `intake` 的 `hash_collision` 改为整批 `failed` 并落 `hash_collisions-<batch>.json`（双路径/双 SHA256）；
  `reconvert` 与 `purge_origin` 实际写盘接入 `kb.lock`。修复 `privacy_scan` 命中的文档绝对路径。
- **阶段 B 异常恢复与运维**：`batch_label` 只读分支改用 `mode=ro` 且不持写锁，工作文件原子写；
  `embed_questions` staging 改 run_id 标记，启动清理历史残留，活任务/清理失败一律中止；
  `refresh` 每次构建写 `build/manifests/refresh/<build_id>.json`，发布后当前授权标记 consumed，
  `--swap-only` 只认 pending 当前清单。
- **阶段 C RAG 质量**：新增 `eval/golden_query_schema.yaml` 与 50 条 `eval/golden_queries.yaml`
  （首版候选 `reviewed: false`，待人工审核）；`eval_retrieval` 新增 Recall@5/MRR@10/nDCG@10/
  无答案误命中率/分主题召回率与 `--golden`/`--report`；`refresh` 新增 G9 软门（未审只告警，
  审核后 Recall@5 低于 0.10 判 FAIL）。`search.rag_contexts` 增加精确 + 同簇软去重、来源聚合、
  `max_context_chars=6000` 全局预算并保留 `q_key`/来源。新增 BGE 单元契约测试与真实模型夜间集成
  工作流（`.github/workflows/bge-integration.yml`，模型缓存 + `RUN_BGE_INTEGRATION=1`）。
- **5,000 题预生产一键试跑**：新增 `mianjing_kb/pilot5000.py`（`prepare`/`run`/`report`）
  与 `scripts/pilot5000.ps1`，以 `D:\pilot\5000\config.json` 为唯一配置入口；`prepare` 只读生产库、
  SQLite Backup API 复制副本并按目标题数估算抽 121 文档（4,544 题），`run` 串起
  extract→canonical→refresh→检索冒烟/评估，`--with-label`/`label` 追加 100 题打标冒烟。首轮
  副本实测 refresh PASS（4,544 题 / 4,544 向量）。新增 `tests/test_pilot5000.py`。同时更新
  `docs/35` 口径（按目标题数截断，topic 为空不做分层）。
- **5,000 题预生产方案 + 标签数据结构复核**：新增 `docs/35`（切片/配置/执行/校验，明确只能影子跑）
  与 `docs/36`（12 个一级领域、稳定 code、7 题型、3 难度 band、quality_state/flags 拆分、
  增量迁移 018 与双写方案）。结论是标签 v2 落库前不跑 5,000 全量打标。
- **关键技术决策 ADR 冻结**：新增 `docs/34-关键技术决策ADR-2026-11-19.md`，把外部建议 D-01～D-11
  正式编为 D-27～D-37（避免覆盖既有 D-01～D-26）。覆盖生产迁移门禁、文件锁、hash_collision、
  staging 清理、发布 manifest、Golden Query、RAG 去重/预算、BGE 集成回归；D-27 已于 2026-11-19
  完成生产 017/qnorm/canonical/检索重建。
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
