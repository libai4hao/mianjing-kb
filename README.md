# mianjing-kb

面经 / 题库知识库管线：**接入 → 分类 → 切题 → 建库 → 混合检索**，附质量门禁与人工复核链路。

仓库只放可版本化的代码、规则与文档；语料原文、SQLite 库留在本机语料根（已被 `.gitignore` 排除）。

**平台现状：仅在 Windows 上实测。** 核心管线（intake / classify / chunker / build_db / search）
的跨平台支持仍属**计划中**，没有 CI 验证；`.doc` 转换与 OCR 依赖 Word COM、PaddleOCR-json，仅 Windows 可用。

## 安装

```powershell
python -m pip install -r requirements.txt      # PyYAML / pymupdf / fastembed / sqlite-vec / pywin32(仅 Windows)
python -m pip install -e .                    # 装出 `kb` 命令入口（等价于 python tools/<step>.py）
```

Python ≥ 3.11。OCR 不是 pip 包：需另装 [PaddleOCR-json](https://github.com/hiroi-sora/PaddleOCR-json)
并用 `KB_OCR_ENGINE_EXE` 指过去。

## 快速上手

```powershell
kb demo                                  # 用 examples/demo_corpus 的合成语料跑通「接入→分类→切题→检索」，不碰生产库
kb intake --db build\kb.sqlite           # 登记 + sha256 判重（新增/重复/移动/改名/隔离）
kb classify --db build\kb.sqlite         # 规则判类 + 近邻投票；--redo 改规则后重判
kb gate --md-dir "%KB_CORPUS_ROOT%\20-题库MD" --db build\kb.sqlite   # 文本质量三态门禁
python -m pytest tests -q                # 全量测试（含 e2e demo、聚类对抗、高危工具守护）
```

`kb --help` 列出全部子命令（`kb list` 是同一份映射表）。需要真实语料时先设环境变量：

```powershell
[Environment]::SetEnvironmentVariable('KB_CORPUS_ROOT','<CORPUS_ROOT>','User')
[Environment]::SetEnvironmentVariable('KB_OCR_ENGINE_DIR','<OCR_ENGINE_DIR>','User')   # 只跑 OCR 才需要
```

未设时工具直接报路径不存在（快速失败，不会乱读别的目录）。其余可覆盖项：
`KB_BUILD_DIR` / `KB_DB` / `KB_META_DIR`。文档里的 `<CORPUS_ROOT>`、`<REPO_ROOT>` 等都是占位符，
对应关系见下表。

| 占位符 | 环境变量 | 含义 |
|---|---|---|
| `<CORPUS_ROOT>` | `KB_CORPUS_ROOT` | 语料根（必填） |
| `<OCR_ENGINE_DIR>` / `<OCR_ENGINE_EXE>` | `KB_OCR_ENGINE_DIR` / `KB_OCR_ENGINE_EXE` | OCR 引擎 |
| `<BUILD_DIR>` | `KB_BUILD_DIR` | 台账 / 库 / 日志目录 |
| `<REPO_ROOT>` / `<PYTHON_HOME>` | — | 仓库根 / 解释器（叙述用） |

## 文档

- `docs/00-决策记录.md` — 关键取舍（含"仓库放在 vibe-work 下"的偏离说明 D-09）
- `docs/04-工具链SOP.md` — 日常操作手册
- `docs/06-部署方案_v2-R3.md` — 当前部署口径（R1/R2 为历史稿）
- `docs/21-基准数据.md` — 历史实测基线（登记速度、分类分布、门禁命中…），**不是现状**
- `docs/22-工程细节.md` — 完整目录、与语料内既有管线的分工、未实现清单
- `docs/项目审核报告/待办清单-2026-11-19.md` — 外部 review 意见的落地台账

## 许可

源码与文档按 MIT 授权，见 `LICENSE`。仓库**不包含**任何面经 / 题库原文及其派生数据：
语料、SQLite 库、`meta/skip_log.json`（含来源文件名的语料清单）都在本机并被排除；
`meta/skip_log.example.json` 只是无来源信息的模板。文档中出现的书名、题目版权归原作者，
仅作工程说明，不构成转载。
