# -*- coding: utf-8 -*-
"""chunker: 面经 md → 一题一块（纯函数 chunk(md_text)->dict，仅 stdlib）。

输出 schema
-----------
{
  "blocks": [
    {"qid": int|None, "q_text": str, "a_text": str,
     "style": "题号|标题|题号/续",
     "source_span": [start_char, end_char],   # 相对原文 md_text 的字符区间
     "icons": ["![alt](path)", ...]}         # 块内图片引用（占位图归属前一块）
  ],
  "leftovers": [{"span": [s, e], "why": str}]  # 未识别/剔除区域，原文一字不丢
}

规则（tests/fixtures 8 组与之对应）
----------------------------------
R1  题号必须原文存在：qid 取自行内数字/汉字数字（一、二…→1,2…），绝不生成。
R2  答案内编号列表不判新题：紧跟非空行的编号行视为上一题的列表项；
    空行后但编号不构成 last_qid+1 递进、也不满足"1. 分节重启/问号跳号"的，同样并入答案。
R3  代码块（``` 围栏）与表格行（| 开头）内部一律不切。
R4  目录行（点线+行尾页码）→ leftovers(why=目录行)，不产出题目。
R5  页眉页脚（第N页 / - N -）、文档头（# 一级标题）、来源引用块（> 开头）→ leftovers。
R6  每块 source_span 可回溯源文；blocks 与 leftovers 区间合并覆盖全文。
R7  块区间内的图片引用计入该块 icons（占位图天然归属前一个题块）。
R8  单题 > 1200 token 时按空行段落续切：续块 style 加 "/续"、qid 同原题、q_text=""
    （`chunk(split_long=False)` 可关闭；chunk_v2 用关闭模式，避免续并后 a_text/a_segs 错位）。
R9  标题粘连（"## 1. A…2. B"）：行内出现 n==qid+1 的编号时原地拆成两题。
R10 首题之前、无法归类的编号内容 → leftovers（绝不并入上一题）。

chunk_v2(md_text) —— 切题器 v2 规范（2026-11-17 拍板，放宽判题信号 + 自检）
------------------------------------------------------------------
什么算一题（满足任一，不要求有题号）：
  1) 明确题号（v1 已有）；2) 问句：独立行以？结尾或含 什么是/如何/为什么/区别/谈谈/介绍/说说/有没有；
  3) 标题+讲解（v1 标题块）；4) 考点+答案："知识点：/考点：" 行。
什么不算：目录/代码块/表格/页眉页脚/水印/致谢·招聘广告（promo 行→leftovers why=致谢/广告）。
R2' 答案内部的列举编号与句中问词不判新题：只切"空行前置的独立问句行"；
     `区别：/如何实现：` 等冒号小节与归一化后不足 6 字的碎片留在父答案，不另起题。
输出：{"items":[{"seq","q","a","span","qtype","conf"}], "probe":[{"span","why"}],
       "coverage":float, "note":"扣除明细"}
  - qtype ∈ 八股|算法|项目|场景|HR（关键词启发式）
  - conf：high(题号/问句) / low(标题/考点) / none
  - coverage = (items 覆盖 − span 内噪声) / (全长 − 可扣除)，扣除=目录/代码/表格/页码/引用/水印/致谢
    （同一噪声不能既算覆盖又从分母扣，否则会 >1.0）
  - items 为空时 probe 必须非空（1–3 个候选区块）
render(md_text)：两步输出（第一步纯文本清单 ≤12 字/行，第二步 JSON）。

已知局限（诚实声明）
--------------------
- 空行后跳号且无问号的行按答案列表处理（并入当前题块），极端样本可能漏题；
  漏题内容仍在题块 a_text 内、源 span 可回溯，不会丢弃。
- 无编号标题产出 qid=None 的"标题"块；正文型文档（如实操篇）会大量出现。
- 数字+小数（"1.5 倍"）用负向断言排除，但"1. 5 种"这类带空格的仍按题号对待。
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

RE_NUM_DOT = re.compile(r'^\s*(?:#{1,6}\s+)?(\d{1,3})\s*[.、．]\s*(.+?)\s*$')           # 1. 2、 3．
RE_NUM_PAREN = re.compile(r'^\s*(?:#{1,6}\s+)?[（(](\d{1,3})[)）]\s*(.+?)\s*$')         # (1)
RE_NUM_TAIL = re.compile(r'^\s*(?:#{1,6}\s+)?(\d{1,3})\s*[)）]\s*(.+?)\s*$')           # 1) 1）
RE_DI = re.compile(r'^\s*(?:#{1,6}\s+)?第\s*(\d{1,3})\s*题[:：]?\s*(.*?)\s*$')          # 第N题
RE_HAN = re.compile(r'^\s*(?:#{1,6}\s+)?([一二三四五六七八九十]{1,3})\s*、\s*(.+?)\s*$')  # 一、
RE_HEADING = re.compile(r'^\s*(#{1,6})\s+(.+?)\s*$')
RE_DECIMAL = re.compile(r'^\s*(?:#\s*)?\d{1,3}\s*[.、．]\d')                             # 1.5 小数排除
RE_TOC = re.compile(r'[.．·•…]{3,}\s*\d{1,4}\s*$')
RE_PAGE = re.compile(r'^\s*(?:第\s*\d{1,4}\s*页(?:\s*[/共]\s*\d{1,4}\s*页)?|[-—–]\s*\d{1,4}\s*[-—–]?|\d{1,4}|=+\s*page\s*\d{1,4}\s*=+)\s*$', re.I)
RE_IMG = re.compile(r'!\[[^\]]*\]\([^)]+\)')
RE_GLUED = re.compile(r'(?:^|[\u4e00-\u9fff。？?!，,、；;：:\s])(\d{1,3})\s*[.、．]\s*(?=[^\d\s])')

# ---- v2 信号 ----
RE_INTERROGATIVE = re.compile(r'什么是|如何|为什么|区别|谈谈|介绍|说说|有没有|怎么办|怎么实现')
RE_QEND = re.compile(r'[?？]\s*$')
RE_KPOINT = re.compile(r'^\s*(?:#{1,6}\s*)?(?:知识点|考点|面试点)[:：]\s*(.+?)\s*$')
# 推广行：强特征（链接/二维码/领取方式）直接剔除；弱词（招聘/广告/公众号…）只在不
# 像题目时才剔除，避免把「如何设计广告投放系统」「招聘系统怎么设计？」当广告。
RE_PROMO_STRONG = re.compile(r'https?://|www\.|二维码|加微信|扫码(?:关注|领取|进群|加)|免费领取|资料领取')
RE_PROMO_WEAK = re.compile(r'公众号|关注我|感谢阅读|欢迎关注|加微信|联系方式|内推|简历投递|付费|星球|更多资源|招聘信息|招聘岗位|广告合作|推广合作')
# v2 增强（2026-11-17，解锁 432 件讲解型 md）：整行粗体剥壳 + 问题N 题号形态
RE_BOLD_LINE = re.compile(r'^\*\*(.+?)\*\*\s*:?\s*$')
RE_PROBLEM_N = re.compile(r'^\s*(?:#{1,6}\s+)?问题\s*([一二三四五六七八九十]{1,3}|\d{1,3})\s*[：:．.]?\s*(.*)$')
# `*` 分支负向前瞻：`**粗体行**` 不再被误判为列表项（曾挡住粗体问句信号）
RE_LIST_ITEM = re.compile(r'^\s*(?:[-+•]|(?!\*)\*|[>|\]]|#{1,6}\s|\d{1,3}\s*[.、．)）]|[（(]\d{1,3}[)）])')


def strip_bold(s: str) -> str:
    """整行粗体剥壳：`**xxx**` → xxx（含尾冒号容差）；非整行粗体返回原文。"""
    m = RE_BOLD_LINE.match(s)
    return m.group(1).strip() if m else s


def _is_qline(s: str) -> bool:
    """v2 问句行判定：以？结尾，或问词出现在行首 8 字内（防"做详细介绍"类行中误命中）。
    整行粗体先剥壳（`**说说 Spring AOP**` → `说说 Spring AOP`）。"""
    s = strip_bold(s.strip())
    if RE_QEND.search(s):
        return True
    return RE_INTERROGATIVE.search(s[:8]) is not None


def _looks_questionish(s: str) -> bool:
    """推广过滤用：像题目/问句的行不按广告剔除（不要求空行等块级条件）。"""
    s = strip_bold(s.strip())
    return bool(RE_QEND.search(s) or RE_INTERROGATIVE.search(s[:8]) or RE_KPOINT.match(s))


_QNORM_CLEAN = re.compile(r"[^\u4e00-\u9fa5a-z0-9]+")


def _qnorm_len(s: str) -> int:
    """与 question_freq.normalize 同口径的可见文本长度，用于挡住会被 extract 丢弃的碎片。"""
    return len(_QNORM_CLEAN.sub("", s.lower()))


def _is_promo(s: str) -> bool:
    """致谢/招聘/推广短行判定：强特征直接命中；弱词只在不像题目时命中。"""
    if len(s) > 40:
        return False
    if RE_PROMO_STRONG.search(s):
        return True
    return bool(RE_PROMO_WEAK.search(s)) and not _looks_questionish(s)

_HAN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
        "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
        "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20}
for _t in "二三四五六七八九":
    _HAN[f"{_t}十"] = int("一二三四五六七八九".index(_t) + 2) * 10
    for _u in "一二三四五六七八九":
        _HAN[f"{_t}十{_u}"] = int("一二三四五六七八九".index(_t) + 2) * 10 + \
            int("一二三四五六七八九".index(_u) + 1)

MAX_TOKENS = 1200


def _tokens(text: str) -> int:
    cjk = len(CJK.findall(text))
    return cjk + (len(text) - cjk) // 4


def _match_question(line: str) -> Optional[Tuple[Optional[int], str, str]]:
    """返回 (qid|None, q_text, style)；style ∈ {题号, 标题}。"""
    line = strip_bold(line)          # 整行粗体题号（**1. 什么是 X**）先剥壳再匹配
    if RE_DECIMAL.match(line):
        return None
    for pat in (RE_NUM_DOT, RE_NUM_PAREN, RE_NUM_TAIL):
        m = pat.match(line)
        if m:
            return int(m.group(1)), m.group(2), "题号"
    m = RE_DI.match(line)
    if m:
        return int(m.group(1)), m.group(2), "题号"
    m = RE_PROBLEM_N.match(line.strip())      # 问题N 形态（面向字节/连环炮系列主流题号）
    if m:
        body = m.group(2).strip()
        if body:                              # 问题一：正文 → 题号块
            qid = int(m.group(1)) if m.group(1).isdigit() else _HAN.get(m.group(1), 0)
            return qid, body, "题号"
        return None                           # 纯标记行：由调用方 last_problem_mark 武装下一行
    m = RE_HAN.match(line)
    if m and m.group(1) in _HAN:
        return _HAN[m.group(1)], m.group(2), "题号"
    m = RE_HEADING.match(line)
    if m:
        return None, m.group(2), "标题"
    return None


def chunk(md_text: str, *, split_long: bool = True) -> Dict:
    lines = md_text.splitlines(keepends=True)
    n = len(lines)
    starts = [0]
    for ln in lines:
        starts.append(starts[-1] + len(ln))
    starts = starts[:n + 1]
    total = starts[n]

    # ---- 第一遍：行分类 ----
    kinds: List[str] = ["text"] * n
    in_code = False
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("```"):
            in_code = not in_code
            kinds[i] = "code"
            continue
        if in_code:
            kinds[i] = "code"
            continue
        if s.startswith("|"):
            kinds[i] = "table"
            continue
        if s.startswith(">"):
            kinds[i] = "quote"
            continue
        if _is_promo(s):   # v2: 致谢/招聘广告短行；像题目的「广告系统设计」不剔
            kinds[i] = "ack"
            continue
        if RE_TOC.search(s):
            kinds[i] = "toc"
            continue
        if RE_PAGE.match(s) and len(s) <= 30:
            kinds[i] = "page"
            continue
        if re.match(r'^#\s+\S', ln):
            kinds[i] = "doc_header"
        # 配图段标题（convert 派生的结构段）：非文档头也非题，整段落 leftovers
        if re.match(r'^#{1,6}\s+配图', ln):
            kinds[i] = "doc_header"

    # 水印：≥3 次重复出现的短正文行；排除 `答案：/优点：` 这类结构化标签和像题目的行
    def _watermark_candidate(t: str) -> bool:
        t = t.strip()
        return (0 < len(t) <= 12 and not t.endswith(("：", ":"))
                and not RE_LIST_ITEM.match(t) and not _looks_questionish(t))

    cnt = Counter(ln.strip() for i, ln in enumerate(lines)
                  if kinds[i] == "text" and _watermark_candidate(ln.strip()))
    watermark = {t for t, c in cnt.items() if c >= 3}

    # ---- 第二遍：题块起点 ----
    bounds: List[dict] = []
    last_qid: Optional[int] = None
    prev_blank = True
    last_problem_mark = False   # v2 增强：问题N 纯标记行武装紧随的独立问句行
    for i, ln in enumerate(lines):
        if kinds[i] != "text" or ln.strip() in watermark:
            # 页码/目录/页眉/水印只是"扣除区"，不能消费块级空行；否则 `12\n3. 下一题`
            # 会把空行前置条件清掉，导致下一题被并进上一题。
            if not ln.strip():
                prev_blank = True
            elif kinds[i] == "doc_header":
                prev_blank = True     # 标题/文档头也是块边界，下一行可新开题
            last_problem_mark = False
            continue
        s = ln.strip()
        if not s:
            prev_blank = True
            last_problem_mark = False
            continue
        q = _match_question(ln)
        if q is None:
            # v2 增强（2026-11-17）：问题N 纯标记行（`问题一` / `问题一：`）→ 武装下一行
            pm = RE_PROBLEM_N.match(s)
            if pm and not pm.group(2).strip():
                last_problem_mark = True
                prev_blank = False
                continue
            # v2 增强：独立问句行（含整行粗体剥壳）成为块起点——与 _split_answers 的
            # R2' 同口径（空行前置或问题N 武装；非列表项、非小数、长度受限）。
            # 解锁讲解型 md（面向字节/连环炮系列：无数字题号，题干即独立问句行）。
            inner = strip_bold(s)
            if ((prev_blank or last_problem_mark) and 0 < len(inner) <= 160
                    and not RE_LIST_ITEM.match(inner) and not RE_DECIMAL.match(inner)
                    and not inner.rstrip().endswith(("：", ":"))
                    and _is_qline(inner)):
                bounds.append({"line": i, "parts": [(None, inner)], "style": "问句"})
            last_problem_mark = False
            prev_blank = False
            continue
        qid, qtext, style = q
        if style == "标题":
            bounds.append({"line": i, "parts": [(None, qtext)], "style": "标题"})
            # 标题是块边界：下一行编号即使紧贴标题也应能新开题，不能被当成续行。
            prev_blank = True
            last_problem_mark = False
            continue
        new_q = False
        if RE_PROBLEM_N.match(s):
            new_q = True          # 问题N：作者显式标记的题号，不依赖空行/递进口径
        elif last_qid is None:
            new_q = True
        elif prev_blank:
            # 新题（独立行）一律要求空行前置；行内递进由 R9 粘连拆分处理。
            if qid == last_qid + 1:
                new_q = True
            elif qid == 1 and re.search(r'[?？]', qtext):
                new_q = True          # 分节重启
            elif qid > last_qid + 1 and re.search(r'[?？]\s*$', qtext):
                new_q = True          # 跳号且问号收尾
        if new_q:
            # R9 标题粘连拆分
            parts, cur_qid, rest = [], qid, qtext
            for m in RE_GLUED.finditer(qtext):
                num = int(m.group(1))
                if num == cur_qid + 1:
                    parts.append((cur_qid, rest[:m.start(1)].rstrip()))
                    cur_qid, rest = num, qtext[m.start(1):]
            parts.append((cur_qid, rest))
            bounds.append({"line": i, "parts": parts, "style": "题号"})
            last_qid = parts[-1][0]
        prev_blank = False
        last_problem_mark = False

    # ---- 组块 + leftovers ----
    blocks: List[dict] = []
    leftovers: List[dict] = []

    def emit_gap(lo_line: int, hi_line: int):
        """[lo_line, hi_line) 的非题内容 → leftovers（合并同类相邻行）。"""
        runs: List[Tuple[str, int, int]] = []
        for i in range(lo_line, hi_line):
            k = kinds[i]
            s = lines[i].strip()
            if not s and runs:
                runs[-1] = (runs[-1][0], runs[-1][1], i + 1)   # 空行并入当前 run
                continue
            why = {"page": "页码", "toc": "目录行", "doc_header": "文档头",
                   "quote": "引用块(来源)", "code": "块前代码", "table": "块前表格",
                   "ack": "致谢/广告"
                   }.get(k, None)
            if why is None:
                why = "水印" if (k == "text" and s in watermark) else "未识别正文"
            if runs and runs[-1][0] == why:
                runs[-1] = (why, runs[-1][1], i + 1)
            else:
                runs.append((why, i, i + 1))
        for why, a, b in runs:
            leftovers.append({"span": [starts[a], starts[b] if b < n else total], "why": why})

    cur_line = 0
    for bi, bd in enumerate(bounds):
        i = bd["line"]
        if i > cur_line:
            emit_gap(cur_line, i)
        end_line = bounds[bi + 1]["line"] if bi + 1 < len(bounds) else n
        b_start, b_end = starts[i], starts[end_line] if end_line < n else total
        # 答案区：剔除页码/目录行/水印（→ leftovers，span 保留在块区间内）
        inc, exc_runs, inc_idx = [], [], []
        for j in range(i + 1, end_line):
            k = kinds[j]
            why = None
            if k == "page":
                why = "页码"
            elif k == "toc":
                why = "目录行"
            elif k == "ack":
                why = "致谢/广告"
            elif kinds[j] == "text" and lines[j].strip() in watermark:
                why = "水印"
            if why:
                if exc_runs and exc_runs[-1][0] == why and exc_runs[-1][2] == j:
                    exc_runs[-1] = (why, exc_runs[-1][1], j + 1)
                else:
                    exc_runs.append((why, j, j + 1))
            else:
                inc.append(lines[j])
                inc_idx.append(j)
        for why, a, b in exc_runs:
            leftovers.append({"span": [starts[a], starts[b] if b < n else total], "why": why})
        # 行级原文偏移：a_text 剔掉噪声行后长度会变，span 必须按"行 → 原文"映射（不能累计长度）
        a_segs = [(starts[j], starts[j + 1] if j + 1 < n else total) for j in inc_idx]
        a_text = "".join(inc).rstrip("\n")
        seg = md_text[b_start:b_end]
        for j, (qid, qtext) in enumerate(bd["parts"]):
            blocks.append({
                "qid": qid,
                "q_text": qtext,
                "a_text": a_text if j == len(bd["parts"]) - 1 else "",
                "style": bd["style"] if j == 0 else "题号",
                "source_span": [b_start, b_end],
                "a_segs": a_segs,
                "icons": RE_IMG.findall(seg),
            })
        cur_line = end_line
    if cur_line < n:
        emit_gap(cur_line, n)

    if not split_long:
        # v2 用完整块：R8 续块一旦在 v2 合并回去，a_text 与 a_segs 行映射会错位；
        # 长答案按原块落一条题，span 仍精确覆盖原文。
        return {"blocks": blocks, "leftovers": leftovers}

    # ---- R8 超长块续切 ----
    out: List[dict] = []
    for b in blocks:
        if _tokens(b["q_text"] + b["a_text"]) <= MAX_TOKENS or not b["a_text"]:
            out.append(b)
            continue
        head_end = b["source_span"][0] + head_len_of(md_text, b["source_span"][0])
        parts = _paras_with_spans(b["a_text"], head_end)
        runs, acc, acc_tok, run_start = [], "", 0, None
        for ptext, (ps, pe) in parts:
            pt = _tokens(ptext)
            if acc and acc_tok + pt > MAX_TOKENS:
                runs.append((acc, [run_start, ps]))
                acc, acc_tok, run_start = "", 0, None
            if not acc:
                run_start = ps
            acc += ptext
            acc_tok += pt
        if acc:
            runs.append((acc, [run_start, b["source_span"][1]]))
        for j, (atext, span) in enumerate(runs):
            nb = dict(b)
            nb.pop("a_segs", None)      # a_text 已被再切，行级映射失效 → 走旧口径（R8 块仅用于续并）
            if j == 0:
                nb["a_text"], nb["source_span"] = atext, [b["source_span"][0], span[1]]
            else:
                nb["q_text"], nb["a_text"] = "", atext
                nb["style"] = b["style"] + "/续"
                nb["source_span"] = list(span)
            out.append(nb)
    return {"blocks": out, "leftovers": leftovers}


def head_len_of(md_text: str, start: int) -> int:
    """块首行（题目行）长度。"""
    nl = md_text.find("\n", start)
    return (nl - start + 1) if nl != -1 else (len(md_text) - start)


def _paras_with_spans(a_text: str, base: int):
    """a_text 按空行切段 → [(段文本, [s,e])]，span 相对原文。"""
    parts, pos = [], 0
    for m in re.finditer(r'\n[ \t]*\n', a_text):
        parts.append((a_text[pos:m.end()], (base + pos, base + m.end())))
        pos = m.end()
    parts.append((a_text[pos:], (base + pos, base + len(a_text))))
    return parts


# ============================ chunk_v2 ============================

_QTYPE_RULES = [
    ("HR", re.compile(r'自我介绍|介绍(一下|下)?自己|优缺点|职业规划|离职|薪资|加班|反问|为什么选择|团队合作|抗压|兴趣爱好|最大的收获|性格')),
    ("算法", re.compile(r'算法|复杂度|链表|二叉|动态规划|排序|手写|手撕|[Ll]eet[Cc]ode|数组|栈和队列|拓扑')),
    ("项目", re.compile(r'项目|简历|你负责|遇到的难点|架构演进|上线流程')),
    ("场景", re.compile(r'如何设计|怎么设计|设计一个|设计一套|线上|生产环境|排查|宕机|秒杀|高并发|怎么办|突发|限流')),
]
_QTYPE_DEFAULT = "八股"


def classify_qtype(q_text: str) -> str:
    for t, rx in _QTYPE_RULES:
        if rx.search(q_text):
            return t
    return _QTYPE_DEFAULT


def _conf_of(style: str, q_text: str) -> str:
    if style in ("题号", "问句"):
        return "high"
    return "high" if _is_qline(q_text) else "low"


def _split_answers(a_text: str, a_base: int, a_segs: list | None = None):
    """v2 后置切分：a_text 内"空行前置的独立问句/考点行"另起新题（R2' 守卫）。

    a_segs = 该块每行在原文里的 (start, end)（chunk() 落库，行序为剔除噪声前的原始行序）。
    给了 a_segs 就按行映射 span（精确）；没给则退化为 a_text 累计偏移的旧口径
    （a_text 剔过页码/目录行，累计长度会整体前移，仅作兼容兜底）。
    返回 (head_a_text, new_items)；new_items 的 q 为原文行文本。
    """
    lines = a_text.splitlines(keepends=True)
    starts, pos = [], 0
    for ln in lines:
        starts.append(pos); pos += len(ln)
    starts.append(pos)
    segs: list[tuple[list[int], int, int]] = []
    prev_blank, in_code, cur = True, False, []

    def flush(end_rel: int) -> None:
        if cur:
            segs.append((list(cur), starts[cur[0]], end_rel))

    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("```"):
            in_code = not in_code
            cur.append(i); continue
        if in_code:
            cur.append(i); continue
        inner = strip_bold(s)      # v2 增强：粗体问句行与普通问句行同口径判定
        if (prev_blank and 0 < len(inner) <= 120 and not RE_LIST_ITEM.match(inner)
                and not RE_DECIMAL.match(inner)
                and not inner.rstrip().endswith(("：", ":"))
                and _qnorm_len(inner) >= 6
                and (RE_KPOINT.match(inner) or _is_qline(inner))):
            flush(starts[i])
            cur = [i]
        else:
            cur.append(i)
        prev_blank = not s
    flush(len(a_text))
    if not segs:
        return a_text, []
    out = [a_text[s:e] for _idx, s, e in segs]
    head = out[0]
    new_items = []
    for (idxs, s, e), seg in zip(segs[1:], out[1:]):
        first_line = strip_bold(seg.split("\n", 1)[0].strip())   # 题干剥粗体壳
        m = RE_KPOINT.match(first_line)
        new_items.append({"q": (m.group(1) if m else first_line)[:200], "a": seg,
                          "style": "考点" if m else "问句", "_idxs": idxs})
    for it in new_items:
        idxs = it.pop("_idxs")
        if a_segs and idxs[-1] < len(a_segs):
            it["span"] = [a_segs[idxs[0]][0], a_segs[idxs[-1]][1]]
        else:
            it["span"] = [a_base + starts[idxs[0]], a_base + starts[idxs[-1] + 1]]
    return head, new_items


def _merge_intervals(spans: List[Sequence[int]]) -> List[Tuple[int, int]]:
    """区间并集（相邻/重叠合并），用于 coverage 不重复计数。"""
    out: List[Tuple[int, int]] = []
    for s, e in sorted((int(s), int(e)) for s, e in spans if e > s):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _span_len(spans: List[Sequence[int]]) -> int:
    return sum(e - s for s, e in _merge_intervals(spans))


def _overlap_len(a: List[Sequence[int]], b: List[Sequence[int]]) -> int:
    """两个区间集的交集总长。"""
    aa, bb = _merge_intervals(a), _merge_intervals(b)
    i = j = total = 0
    while i < len(aa) and j < len(bb):
        lo, hi = max(aa[i][0], bb[j][0]), min(aa[i][1], bb[j][1])
        if lo < hi:
            total += hi - lo
        if aa[i][1] < bb[j][1]:
            i += 1
        else:
            j += 1
    return total


def chunk_v2(md_text: str) -> Dict:
    """切题器 v2：items/probe/coverage/note（判据见模块 docstring）。"""
    res = chunk(md_text, split_long=False)
    items: List[dict] = []
    seq = [0]

    def add(q, a, span, style):
        seq[0] += 1
        items.append({"seq": seq[0], "q": q, "a": a, "span": list(span),
                      "qtype": classify_qtype(q), "conf": _conf_of(style, q)})

    for b in res["blocks"]:
        if b["style"].endswith("/续") and items:          # 续块并回上一题
            items[-1]["a"] = (items[-1]["a"] + "\n" + b["a_text"]).strip("\n")
            items[-1]["span"][1] = b["source_span"][1]
            continue
        a_rest, extra = _split_answers(b["a_text"], b["source_span"][0], b.get("a_segs"))
        end = extra[0]["span"][0] if extra else b["source_span"][1]
        add(b["q_text"], a_rest, [b["source_span"][0], end], b["style"])
        for e in extra:
            add(e["q"], e["a"], e["span"], e["style"])

    # probe：items 为空时给 1–3 个候选区块
    probe = []
    if not items:
        lines = md_text.splitlines(keepends=True)
        starts, pos = [], 0
        for ln in lines:
            starts.append(pos); pos += len(ln)
        starts.append(pos)
        in_code = False
        for i, ln in enumerate(lines):
            s = ln.strip()
            if s.startswith("```"):
                in_code = not in_code; continue
            if in_code or RE_TOC.search(s) or RE_PAGE.match(s) or not s:
                continue
            if (_match_question(ln) or RE_KPOINT.match(s)
                    or ((RE_QEND.search(s) or RE_INTERROGATIVE.search(s)) and len(s) <= 160)):
                why = ("题号行" if _match_question(ln) else
                       "考点行" if RE_KPOINT.match(s) else "问句行")
                probe.append({"span": [starts[i], starts[i + 1]], "why": why})
                if len(probe) >= 3:
                    break

    # coverage：净覆盖 / 可计入总长
    # 2026-11-19 审核修复：扣除区（页码/目录/水印）常常落在 item span 内部，旧口径
    # "covered 累加 + 分母减扣除"两边同时计，会算出 >1.0 的 coverage（实测 1.0612）。
    total = len(md_text)
    item_iv = [it["span"] for it in items]
    deduct_iv: List[Sequence[int]] = []
    deduct_detail: Dict[str, int] = {}
    for l in res["leftovers"]:
        if l["why"] == "未识别正文":
            continue
        s, e = l["span"]
        deduct_iv.append((s, e))
        deduct_detail[l["why"]] = deduct_detail.get(l["why"], 0) + (e - s)
    covered = _span_len(item_iv)
    noise_in_items = _overlap_len(item_iv, deduct_iv)
    deduct = _span_len(deduct_iv)
    net = covered - noise_in_items
    denom = max(1, total - deduct)
    coverage = round(min(1.0, max(0.0, net / denom)), 4) if total else 1.0
    note = "扣除:" + (", ".join(f"{k} {v}" for k, v in sorted(deduct_detail.items())) or "无") \
           + f"; 总长 {total}, 覆盖 {covered}(其中噪声 {noise_in_items}), 净覆盖 {net}, 扣除 {deduct}"
    return {"items": items, "probe": probe, "coverage": coverage, "note": note}


def _summary(q: str) -> str:
    s = re.sub(r'\s+', '', q)
    return s[:12] if s else "(无题干)"


def render(md_text: str) -> str:
    """两步输出：第一步纯文本清单，第二步 JSON。"""
    import json as _json
    v2 = chunk_v2(md_text)
    lines = ["第一步：题目清单"]
    if v2["items"]:
        lines += [f"{it['seq']}. {_summary(it['q'])}" for it in v2["items"]]
    else:
        lines.append("无")
    v2_out = {k: v for k, v in v2.items() if k != "note"}
    lines += ["", "第二步：", _json.dumps(v2_out, ensure_ascii=False, indent=1),
              "", v2["note"]]
    return "\n".join(lines)


def main(argv=None) -> int:
    import argparse
    import json as _json
    ap = argparse.ArgumentParser(description="面经切题器（v2 两步输出）")
    ap.add_argument("file", nargs="?", help="输入 md/txt 文件；缺省读 stdin")
    ap.add_argument("--v1", action="store_true", help="输出 v1 blocks/leftovers schema")
    args = ap.parse_args(argv)
    import sys as _sys
    if args.file:
        raw = Path(args.file).read_bytes()
        for enc in ("utf-8-sig", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", errors="replace")
    else:
        text = _sys.stdin.read()
    if args.v1:
        res_v1 = chunk(text)
        for b in res_v1["blocks"]:
            b.pop("a_segs", None)       # 行级偏移是内部口径，不进 CLI 输出
        print(_json.dumps(res_v1, ensure_ascii=False, indent=1))
    else:
        print(render(text))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
