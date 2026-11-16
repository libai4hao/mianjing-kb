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
R8  单题 > 1200 token 时按空行段落续切：续块 style 加 "/续"、qid 同原题、q_text=""。
R9  标题粘连（"## 1. A…2. B"）：行内出现 n==qid+1 的编号时原地拆成两题。
R10 首题之前、无法归类的编号内容 → leftovers（绝不并入上一题）。

已知局限（诚实声明）
--------------------
- 空行后跳号且无问号的行按答案列表处理（并入当前题块），极端样本可能漏题；
  漏题内容仍在题块 a_text 内、源 span 可回溯，不会丢弃。
- 无编号标题产出 qid=None 的"标题"块；正文型文档（如实操篇）会大量出现。
- 数字+小数（"1.5 倍"）用负向断言排除，但"1. 5 种"这类带空格的仍按题号对待。
"""
from __future__ import annotations

import bisect
import re
from collections import Counter
from typing import Dict, List, Optional, Tuple

CJK = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf]')

RE_NUM_DOT = re.compile(r'^\s*(?:#{1,6}\s+)?(\d{1,3})\s*[.、．]\s*(.+?)\s*$')           # 1. 2、 3．
RE_NUM_PAREN = re.compile(r'^\s*(?:#{1,6}\s+)?[（(](\d{1,3})[)）]\s*(.+?)\s*$')         # (1)
RE_NUM_TAIL = re.compile(r'^\s*(?:#{1,6}\s+)?(\d{1,3})\s*[)）]\s*(.+?)\s*$')           # 1) 1）
RE_DI = re.compile(r'^\s*(?:#{1,6}\s+)?第\s*(\d{1,3})\s*题[:：]?\s*(.*?)\s*$')          # 第N题
RE_HAN = re.compile(r'^\s*(?:#{1,6}\s+)?([一二三四五六七八九十]{1,3})\s*、\s*(.+?)\s*$')  # 一、
RE_HEADING = re.compile(r'^\s*(#{1,6})\s+(.+?)\s*$')
RE_DECIMAL = re.compile(r'^\s*(?:#\s*)?\d{1,3}\s*[.、．]\d')                             # 1.5 小数排除
RE_TOC = re.compile(r'[.．·•…]{3,}\s*\d{1,4}\s*$')
RE_PAGE = re.compile(r'^\s*(?:第\s*\d{1,4}\s*页(?:\s*[/共]\s*\d{1,4}\s*页)?|[-—–]\s*\d{1,4}\s*[-—–]?|\d{1,4})\s*$')
RE_IMG = re.compile(r'!\[[^\]]*\]\([^)]+\)')
RE_GLUED = re.compile(r'(?:^|[\u4e00-\u9fff。？?!，,、；;：:\s])(\d{1,3})\s*[.、．]\s*(?=[^\d\s])')

_HAN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8,
        "九": 9, "十": 10, "十一": 11, "十二": 12, "十三": 13, "十四": 14, "十五": 15,
        "十六": 16, "十七": 17, "十八": 18, "十九": 19, "二十": 20}

MAX_TOKENS = 1200


def _tokens(text: str) -> int:
    cjk = len(CJK.findall(text))
    return cjk + (len(text) - cjk) // 4


def _match_question(line: str) -> Optional[Tuple[Optional[int], str, str]]:
    """返回 (qid|None, q_text, style)；style ∈ {题号, 标题}。"""
    if RE_DECIMAL.match(line):
        return None
    for pat in (RE_NUM_DOT, RE_NUM_PAREN, RE_NUM_TAIL):
        m = pat.match(line)
        if m:
            return int(m.group(1)), m.group(2), "题号"
    m = RE_DI.match(line)
    if m:
        return int(m.group(1)), m.group(2), "题号"
    m = RE_HAN.match(line)
    if m and m.group(1) in _HAN:
        return _HAN[m.group(1)], m.group(2), "题号"
    m = RE_HEADING.match(line)
    if m:
        return None, m.group(2), "标题"
    return None


def chunk(md_text: str) -> Dict:
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
        if RE_TOC.search(s):
            kinds[i] = "toc"
            continue
        if RE_PAGE.match(s) and len(s) <= 14:
            kinds[i] = "page"
            continue
        if re.match(r'^#\s+\S', ln):
            kinds[i] = "doc_header"

    # 水印：≥3 次重复出现的短正文行
    cnt = Counter(ln.strip() for i, ln in enumerate(lines)
                  if kinds[i] == "text" and 0 < len(ln.strip()) <= 12)
    watermark = {t for t, c in cnt.items() if c >= 3}

    # ---- 第二遍：题块起点 ----
    bounds: List[dict] = []
    last_qid: Optional[int] = None
    prev_blank = True
    for i, ln in enumerate(lines):
        if kinds[i] != "text" or ln.strip() in watermark:
            prev_blank = ln.strip() == ""
            if not ln.strip():
                prev_blank = True
            continue
        s = ln.strip()
        if not s:
            prev_blank = True
            continue
        q = _match_question(ln)
        if q is None:
            prev_blank = False
            continue
        qid, qtext, style = q
        if style == "标题":
            bounds.append({"line": i, "parts": [(None, qtext)], "style": "标题"})
            prev_blank = False
            continue
        new_q = False
        if last_qid is None:
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
                   "quote": "引用块(来源)", "code": "块前代码", "table": "块前表格"
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
        inc, exc_runs = [], []
        for j in range(i + 1, end_line):
            k = kinds[j]
            why = None
            if k == "page":
                why = "页码"
            elif k == "toc":
                why = "目录行"
            elif kinds[j] == "text" and lines[j].strip() in watermark:
                why = "水印"
            if why:
                if exc_runs and exc_runs[-1][0] == why and exc_runs[-1][2] == j:
                    exc_runs[-1] = (why, exc_runs[-1][1], j + 1)
                else:
                    exc_runs.append((why, j, j + 1))
            else:
                inc.append(lines[j])
        for why, a, b in exc_runs:
            leftovers.append({"span": [starts[a], starts[b] if b < n else total], "why": why})
        a_text = "".join(inc).rstrip("\n")
        seg = md_text[b_start:b_end]
        for j, (qid, qtext) in enumerate(bd["parts"]):
            blocks.append({
                "qid": qid,
                "q_text": qtext,
                "a_text": a_text if j == len(bd["parts"]) - 1 else "",
                "style": bd["style"] if j == 0 else "题号",
                "source_span": [b_start, b_end],
                "icons": RE_IMG.findall(seg),
            })
        cur_line = end_line
    if cur_line < n:
        emit_gap(cur_line, n)

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
