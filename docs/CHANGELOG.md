# 工程变更日志（CHANGELOG）

> 状态：现行 · 最后更新：2026-11-19 · 适用版本：v2-R3
>
> 只记**工程变更**（代码、门禁、依赖、文档结构），不记数据快照——题数、库大小会随重建漂移，
> 一律以 `docs/29-数据基线与统计口径-2026-11-19.md` 为准。历史里程碑按日期倒序。
> 2026-11-16 之前的记录见 `docs/archive/` 与 git 历史，本表从外部 review 落地开始。

## 2026-11-19

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
