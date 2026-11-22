# 切题逻辑金标问题修复计划（2026-11-22）

## 背景

五轴 code review（报告：`<REVIEW_DIR>\code_review_切题逻辑.md`，仓库外评审辅助目录）用当前 `chunk_v2` 实跑 5 篇金标原文（047/074/075/009/028）与 20 条人工金标对账：9 条已修复、8 条仍复现、3 条属内容质量。8 条复现错误归为 5 类结构性 Must fix。用户已确认"处理"，按 TDD 逐类修复。

涉及文件：
- `mianjing_kb/chunker.py`（1639 行，唯一改动源）
- `tests/test_chunker_gold_regression.py`（新建）
- 辅助验证脚本：`<REVIEW_DIR>\reconcile.py`（仓库外，不提交；`<REVIEW_DIR>` 为评审辅助目录占位符）

## 目标与验收

1. 8 条复现金标错误全部消除（reconcile 复跑确认）：
   - S0496/S0537-幽灵（TOC 条目成题）、S0549/S0537（答案跨节 bleed）、S0517/S0533/S0340/S0348（非题成题）。
2. 已修复的 9 条不回归（S0371/S0375 题干合并、S0528/S0499 目录清洗、S0310 分割线、S0500 屏蔽等）。
3. 现有测试套件全绿（以基线为准，不引入新失败）。
4. 新增 `tests/test_chunker_gold_regression.py` 覆盖 5 类错误的最小复现 fixture。

## 约束

- 纯 stdlib，不新增依赖。
- 不改 `chunk()` / `chunk_v2()` 对外签名与返回结构。
- 每个任务：先写失败测试（RED）→ 最小实现（GREEN）→ 跑 chunker 相关套件 → commit。
- 行为变更必须先有失败测试；纯重构（T6/T7）靠现有套件回归。

## 基线（动手前先跑）

```powershell
$PY = "<PYTHON>"   # 任一可用的 Python 3.11+ 解释器
Set-Location <REPO_ROOT>   # 本仓库根目录
& $PY -m pytest tests -q          # 记录既有失败清单（基线）
& $PY <REVIEW_DIR>\reconcile.py   # 记录当前 8 条复现
```

chunker 相关套件（每任务 GREEN 后必跑）：
`tests/test_chunker.py tests/test_chunker_v2.py tests/test_chunker_accuracy.py tests/test_chunker_structural.py tests/test_chunker_structure_regression.py tests/test_chunker_question_intent.py tests/test_answer_boundary.py tests/test_answer_cleaning.py tests/test_question_quality.py tests/test_chunker_gold_regression.py`

---

## T1 F3：粘连层级节标题导致答案跨节 bleed（S0549/S0537）

**根因**：PDF 转换把层级节标题粘进正文行。
(a) 行中粘连：`…关于Redis事务不是原⼦性问题 ）4.2.9 缓存雪崩…`（闭标点后无换行）→ 节标题混入上一题答案尾部。
(b) 行首粘连：`2.4.2 说⼀下Java对象的创建过程下图便是…`（节头+首句同线）→ `_match_question` 被 `RE_DECIMAL`(chunker.py:91) 拦死返回 None，行无边界 → 后续节内容全部吸入上一题答案。
`RE_HIER_NUM`(126) 只匹配行首 2-3 段式；行内拆分 `_split_inline_hierarchical_lines`(696) 只拆带问句语义的行。

**RED**（`tests/test_chunker_gold_regression.py` 新增）：
- `test_glued_hier_midline_bleed`：题 + 答案 + 粘连行 `…问题 ）4.2.9 缓存雪崩和缓存穿透问题解决方案…`。断言：redis 题答案不含 `4.2.9`、不含 `缓存雪崩`。
- `test_glued_hier_linestart_bleed`：题 + 答案 + `2.4.2 说⼀下Java对象的创建过程下图便是…` + 节内容。断言：上一题答案不含 `2.4.2`、不含 `类加载检查`。
- `test_hier_question_still_binds`：`2.4.6 为什么要将永久代(PermGen)替换为元空间(MetaSpace)呢?` + 答案。断言：产出该题且答案正确（正向用例，防误杀）。

**GREEN**（chunker.py）：
1. `_split_inline_hierarchical_lines`(696-732)：层级号前一个字符为闭标点 `）)】」』。！？!?；;` 时，即使 suffix 无问句语义也拆（suffix 仍要求 len≥4、非 codeish、非虚词开头）。
2. `chunk()` 第二遍（876-1116 内，题号独占处理 924-947 之前）：对 `_match_question` 返回 None 的 text 行，若 `RE_HIER_NUM.match(s)`：
   - 题文（group(2)，NFKC）有问句语义（`_has_question_intent` / `_is_question_heading` / `RE_QEND`）→ 按"标题"style 成 bound（qid=None），`last_qid=None`；
   - 否则 `kinds[i]="section"` 硬边界（内容归 leftovers），`last_qid=None`、`prev_blank=True`。

**验证**：新测试绿 + chunker 套件绿 + reconcile（S0537/S0549 答案不再跨节）→ commit `fix(chunker): 粘连层级节标题不再吸入答案（F3）`。

---

## T2 F1：TOC mask 中途截断，目录条目成题（S0496/幽灵）

**根因**：`_toc_mask`(212-271) 截断条件 248-251 `RE_MD_H12.match(s) and not RE_TOC.search(s) and (leaders>=2 or outline>=3)`——075 的 TOC 自带 `## N.` 条目（行 91 `## 1. ⾯向对象…`），撞上即截断，其后 ~800 行 TOC 裸奔，逃逸条目成题（seq52/53/54/62/63）。

**RED**：
- `test_toc_h12_entries_not_truncating`：fixture = "目录"标题 + 若干带 leaders 的 outline 行 + `## 1. ⾯向对象和⾯向过程的区别` 式 H12 条目 + 其后继续 outline 行（混合 `## N.` 与裸 `1.4.1`）+ 正文真题。断言：无 item 的 q 为 `⾯向对象和⾯向过程的区别`（TOC 独有、正文无），且正文真题正常产出。

**GREEN**（chunker.py:248-251）：命中 H12 且满足 leaders/outline 条件时，不立即截断——先做"目录密度前瞻"：取其后 ~12 个非空行，其中 outline 形态（len≤60 且不以 `。！？!?` 结尾，或 `RE_TOC.search` 命中）占比 ≥ 1/2 → 视为 TOC 条目继续扫（outline+=1）；否则才截断。

**验证**：新测试绿 + 套件绿 + reconcile（S0496 不再产出、seq52-63 类幽灵消失）→ commit `fix(chunker): TOC 内 H12 条目不再提前截断掩码（F1）`。

---

## T3 F2：同题干去重不吞目录幽灵（S0537-幽灵/seq10 vs seq149）

**根因**：`chunk_v2` dedup(1496-1523) 只处理"空答案替换/前缀包含"，TOC 幽灵（垃圾短答案，如 `重载\n\n重写`）与正文真身（2128 字）并存。实测 075 有 14 组同规范题干重复。

**RED**：
- `test_dedup_drops_toc_ghost`：TOC 含 `重载和重写的区别`（其后邻行 `重载`/`重写` 成垃圾答案）+ 正文同题 + 实质答案。断言：该题干只产出 1 条 item，答案含实质内容（如 `发⽣在同⼀个类中`）。

**GREEN**（chunker.py）：
1. 新增 `_toc_ghost_answer(a)`：非空行 1..6 行、每行 len≤24 且不含 `[。！？!?；;：:]` → 幽灵。
2. dedup 三分支改造：
   - 旧空+新有 → 替换（现状）；
   - 旧有+新空：旧是幽灵 → 替换为新的（留正文空答案），否则保留旧（现状）；
   - 都有：一幽灵一实质 → 留实质（旧幽灵→替换；新幽灵→丢弃新）；都幽灵或都实质 → 走现状前缀包含/都保留。

**验证**：新测试绿 + 套件绿 + reconcile（幽灵组消失，正文真身保留）→ commit `fix(chunker): 去重吞掉目录幽灵条目（F2）`。

---

## T4 F4：面试官嵌入问句丢失 + `## 2. 区别 ：` 碎片题偷答案

**根因**：
(a) `3.4 分⻚机制…! ⾯试官 ： 分⻚机制和分段机制有哪些共同点和区别呢？` 整行 `_match_question` 为 None（RE_DECIMAL 拦截 3.4），真问题丢失；随后 `## 2. 区别 ：` 因 `_is_question_heading` 经 RE_INTERROGATIVE 的 `区别` 判 True，绕过 1047 的 subpoint 逻辑成碎片题（seq302），偷走答案后半。
(b) `_is_question_heading("区别 ：")` 为 True 的根因：`_is_qline` 把行首 8 字内的 `区别` 当问词。

**RED**：
- `test_interviewer_embedded_question`：`3.4 分⻚机制和分段机制的共同点和区别! ⾯试官 ： 分⻚机制和分段机制有哪些共同点和区别呢？` + 答案 + `## 1. 共同点 ：` + 内容 + `## 2. 区别 ：` + 内容。断言：产出题 q 含 `分⻚机制和分段机制`；其答案同时含共同点与区别内容；无 q 为 `区别 ：` 的 item。
- `test_short_label_heading_stays_in_answer`：题 + 答案 + `## 1. 共同点 ：` + 内容 + `## 2. 区别 ：` + 内容。断言：无碎片题、内容留在父答案。

**GREEN**（chunker.py）：
1. F4a：第二遍对 `_match_question` 为 None 的 text 行，`re.search(r'(?:⾯试官|面试官|追问|继续问|接着问)\s*[：:]\s*(.+)$', s)` 命中且尾部有问句语义（`_is_qline` 或 `_has_question_intent` 或 `RE_QEND`）→ 按"问句"style 成 bound（qtext=尾部）。
2. F4b：`md_num_heading` 分支（1015 起）最前面加门槛：`_qnorm_len(qtext) < 6` → 列表 skip（留父答案），优先于 `_is_question_heading` 判定（覆盖 `区别 ：`/`共同点 ：`）。

**验证**：新测试绿 + 套件绿 + reconcile（真问题找回、seq302/309/310 碎片消失）→ commit `fix(chunker): 找回面试官嵌入问句、短标签标题不再成碎片题（F4）`。

---

## T5 F5：编号标题/题号顺延无问句语义门槛（S0517/S0533/S0340/S0348）

**根因**：`explicit_question_heading`(1041-1046) 对 `## N.` 无问词要求（注释明言刻意取舍）；`sequential_heading` bypass(1047-1049) 让顺延编号无条件成题。四条金标复现：S0517（学习建议列表项）、S0533（Demo 节头+说明句）、S0340（047 列表项）、S0348（028 叙述行）。

**RED**：
- `test_skill_list_heading_not_question`：`## 4.` 真题 + `## 5. 熟悉如何优化 Java 代码、有写出质量更⾼的代码的意识；` + 内容。断言：无该 item，且真题答案不含其后内容（硬边界）。
- `test_demo_heading_not_question`：`## 5.` 真题 + `## 6. ⼀个简单的线程池Demo: Runnable + ThreadPoolExecutor为了让⼤家更清楚…` + 代码。断言：无该 item，代码不进真题答案。
- `test_short_np_heading_still_question`：非顺延 `## 11. Oracle JDK 和 OpenJDK 的对比` + 答案。断言：仍成题（防误杀）。
- `test_list_lead_steps`：`?我跟大家介绍一下一般面试前的准备步骤：` + `1、了解公司（…）；` + `2、了解职位（…）。` + 真题。断言：无 了解公司/了解职位 item，真题正常。
- `test_narrative_numbered_line_not_question`：`1、上来照例还是问了问项目：` + `答：…` + `2、项目亮点` + `4、字符集和编码的区别？` + 答案。断言：无 q 含 `问了问` 的 item；`字符集和编码的区别` 正常成题。

**GREEN**（chunker.py）：
1. 新增 `_heading_questionish(qtext)`：NFKC；技能列表开头 `^(?:熟悉|了解|掌握|精通|具备)` 且非？结尾 → False；`RE_QEND`/`_has_question_intent`/`RE_QUESTION_SUFFIX`/`_is_question_heading` 任一 → True；否则短名词短语（len≤40 且无 `[，,；;。.！!]`）→ True。
2. 重构 1047-1054（`md_num_heading and not _is_question_heading(qtext)` 时）：
   - (1) `answer_subpoint_restart or answer_label or RE_SECTION_TITLE or RE_LIST_INTRO or _qnorm_len<6` → 列表 skip（现状语义，T4 已加 qnorm 门槛则此条自动满足）；
   - (2) `not _heading_questionish(qtext)` → `kinds[i]="section"` + `last_qid=None`（S0517/S0533 硬边界防 bleed）；
   - (3) 否则落 new_q。
   `explicit_question_heading`(1041-1046) 追加 `and _heading_questionish(qtext)`；旧 `sequential_heading` bypass 被 questionish 取代。
3. F5b：`RE_LIST_LEAD`(122-123) 追加 `|步骤|建议|要点`。
4. F5c：新增 `RE_NARRATIVE_LEAD = re.compile(r'(?:问了问|聊了聊|问了下|然后他就说|他就说|又问了|接着问)')`；第二遍题号 style 行命中且 qtext 以 `：/:` 结尾且无 `？` → `kinds[i]="section"` + `last_qid=None`（028 S0348；`3、然后他就说…` 不以：结尾不受影响）。

**验证**：新测试绿 + 套件绿 + reconcile（S0517/S0533/S0340/S0348 全部不再产出；S0500 类屏蔽不回归）→ commit `fix(chunker): 编号标题加题目语义门槛、列表引导与叙述行不再成题（F5）`。

---

## T6 F7：`_toc_mask` O(n·5000) 重扫（性能，纯重构）

**根因**：chunker.py:258/263 `any(RE_MD_H12.match(x.strip()) for x in lines[j:j+5000])` 在每行循环内重扫 5000 行。075 实测未触发（mask 行 90 即停），但大文档有 O(n·5000) 风险。

**改动**：`_toc_mask` 开头预计算 `h12_idx = [i for i, x in enumerate(lines) if RE_MD_H12.match(x.strip())]`，258/263 两处 any() 换 `bisect.bisect_left(h12_idx, j)` 判断 `[j, j+5000)` 内是否存在。行为不变。

**验证**：无新测试（重构）；chunker 套件全绿 + reconcile 输出与 T5 后一致 → commit `perf(chunker): _toc_mask 前瞻扫描改预计算索引（F7）`。

---

## T7 F6：可读性文档（纯注释）

1. `_heading_questionish` docstring：写明"无问词的短名词短语标题也算题、技能列表/长句叙述不算"的取舍边界。
2. `_local_outline_mask`(274) docstring：补三层掩码分工小表（`_toc_mask`=显式"目录"区；`_local_outline_mask`=无目录字样的局部大纲；`answer_label`/section kinds=答案内小节）。
3. 模块 docstring：已知局限区追加"PDF 行粘连节标题"条目（F3/F4 修复后残余风险：两段式 `3.4` 行首粘连仍依赖 F4a 面试官模式或无边界）。

**验证**：`& $PY -c "import mianjing_kb.chunker"` 无语法错 + 套件绿 → commit `docs(chunker): 掩码分工与语义门槛注释（F6）`。

---

## 收尾验证

1. 全量：`& $PY -m pytest tests -q`，对照基线无新失败。
2. 金标：`& $PY <REVIEW_DIR>\reconcile.py`——8 条复现错误全部消除、9 条已修复不回归、3 条内容质量类不变。
3. 汇报：改动清单、测试结果、reconcile 前后对照、残余风险（两段式粘连残余、金标 100 条中未覆盖的 80 条建议后续全量对账）。

## 风险与回滚

- 每任务独立 commit，出问题 `git revert` 单任务回滚。
- 最大风险：F5 语义门槛误杀其他文档的编号真题（030 类整篇 `## N、题`）。缓解：正向用例（短名词短语仍成题）+ 全量套件 + reconcile；若套件出现误杀，放宽 `_heading_questionish` 的短名词短语阈值（len 上限或标点集）再跑。
- F1 密度前瞻可能把正文开头的大纲式列表误判为 TOC：前瞻只看其后 12 行且要求 outline 形态过半，正文长句（>60 字或带句号）会立即截断；reconcile 的 coverage 对照兜底。
