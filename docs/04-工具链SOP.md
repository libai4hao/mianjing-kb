# 工具链 SOP

面向"下一轮直接照着跑"，每个脚本都给出命令、参数语义、输出解读和排障。

环境：`python`（3.11.4，含 pymupdf / pymupdf4llm / numpy / sklearn / yaml / pytest）。
Shell：pwsh 7（`C:\Program Files\PowerShell\7\pwsh.exe`）。所有中文路径用 `-LiteralPath`。

---

## 0. 一次性准备

```powershell
$py = "python"
$repo = "<WORKSPACE_ROOT>"
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

判定链（`meta/folder_rules.yaml` + `meta/topic_rules.yaml`）：

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

19 个用例覆盖：文本指标与阈值、intake 的新增/重复/改名/复活/同批重复、classify 的 topic/结构位/collection/未分类、gate 的三种 verdict 与报告落盘。

---

## 6. 未实现清单（明确记录，不假装有）

| 组件 | 状态 | 缺口 |
|---|---|---|
| `classify --neighbor` | 未实现 | 需文档向量索引（第②档归因，覆盖规则之后的残渣） |
| `cluster`（增量吸附） | 未实现 | `canonical_questions` / `question_variants` 表已建，0.92/0.85 阈值算法未写 |
| `reconcile`（四数对账） | 未实现 | 磁盘 / 表 / convert_log / DB 四数闭合，用来清 `ok=423 vs 397` 的 26 条旧账 |
| `dispatch.py` | 未实现 | 按 category 选拆题策略（题库→parse_banks，面经→extract，专题→只索引） |
| `extract.py` | 未实现 | 面经抽题 + 公司/年份归一 |
| `30-补录` / `31-收件箱` | 未建目录 | 补录脚本只跑过 `-DryRun`（41 个 doc 命中） |

---

## 7. 已知陷阱

| 现象 | 原因 | 对策 |
|---|---|---|
| `partially initialized module 'inspect'` | `%TEMP%` 根有 zcode 残留 `inspect.py` | 临时脚本放独立子目录（如 `%TEMP%\zocr\`） |
| `Copy-Item` 找不到中文路径文件 | PowerShell 直接写中文路径 | 先 `Get-ChildItem \| Where-Object { $_.Name -like ... }` 取 `FullName` 再操作 |
| sqlite 写入报 locked | WAL 下多写者 | 单写者串行；本仓库脚本都是短事务 |
| 分类判错 | 规则词典（`meta/*.yaml`）而非数据 | 改 yaml → `classify --redo`，**不碰 md、不重跑 OCR** |
