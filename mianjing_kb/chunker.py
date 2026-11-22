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

准确率增强（2026-11-20 重构，docs/37 已知缺陷修复，tests/test_chunker_accuracy.py）
------------------------------------------------------------------------------
D1 题干续行回接：题干以连接词/介词悬空结尾（的/和/与/在/是…），下一内容行是
   问句形态短行（≤60 字、非列表/题号/代码/答案引导）→ 续回题干，不另成假题；
   续行不进 a_text，span/a_segs 行映射保持精确。主通道与 _split_answers 同口径。
D2 代码碎片出口终检：chunk_v2 items 出口过 is_code_fragment，漏过行级判定的
   编号代码行（docx 无围栏形态）不产题；内容并入上一题答案、span 顺延可回溯。
D3 大题说明行（「综合大题（五道选两道，每题 10 分）」）→ 不产题、重置题号；
   答案列表状态不吞「空行前置 + 强提问语义行首（什么是/如何/为什么…）」的编号子题。

review2 结构修复移植（2026-11-20，tests/test_chunker_structure_regression.py）
------------------------------------------------------------------------------
S1 问句/任务语义（行首问词、句尾 吗/呢/哪N个方面、选择题空括号占位）优先于
   题号连续性；显式 Markdown 编号题头即使无问词也判题（答案小节/章节除外）。
S2 TOC：NFKC 兼容 ⽬录；无页码目录靠短大纲条目证据、遇正文 H1/H2 截断；
   层级题号首现后无答案且后文重复 → 局部目录 leftovers。
S3 真章节标题是硬边界：结束答案、重置题号，leftovers(章节标题) 参与 coverage 扣除。
S4 行内粘连层级题号拆虚拟行：前缀留父答案，新题从编号处开始，span 精确回溯。
S5 强证据题干续行（（implements）/class）？）从答案区收回题干，a_segs 同步裁剪。

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

import bisect
import re
import unicodedata
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

# docx 转文本没有 ``` 围栏时，代码会被当正文切成一题一行；这里做兜底识别。
RE_CJK = re.compile(r'[\u4e00-\u9fff]')
RE_CODE_HEAD = re.compile(r'^\s*(?:#\s*(?:include|define|ifdef|ifndef|endif|pragma)\b|//|/\s*\*)')
RE_CODE_CALL = re.compile(
    r'\b(?:printf|fprintf|malloc|calloc|realloc|free|strlen|strcpy|memcpy)\s*[（(]')
RE_CODE_ASSIGN = re.compile(r'^[A-Za-z_]\w*(?:\s*\[[^\]]*\])?\s*=\s*[^=]')

# ---- 文档结构识别（2026-11-20 切题回归修复）----
RE_TOC_HEADER = re.compile(r'^\s*(?:目\s*录|contents|table\s+of\s+contents)\s*$', re.I)
RE_MD_NUM_HEADING = re.compile(r'^\s*#{1,6}\s+\d{1,3}(?:\s*[.、．]|\s*[)）])')
RE_MD_H12 = re.compile(r'^\s*#{1,2}\s+\S')
RE_LIST_LEAD = re.compile(
    r'(?:如下|以下|下列|包括|包含|分为|分成|分别是|基本要素|几个方面|哪几种|哪些|几种|四种|三种'
    r'|步骤(?=\s*[：:])|建议(?=\s*[：:])|要点(?=\s*[：:]))')
# F5c：面经叙述行（“1、上来照例还是问了问项目：”）有题号但整行是
# 叙事，不是题；成题会偷走后面的叙述内容。
RE_NARRATIVE_LEAD = re.compile(r"(?:问了问|聊了聊|问了下|然后他就说|他就说|又问了|接着问)")
RE_NESTED_NUM = re.compile(
    r'^\s*(?:#{1,6}\s+)?((?:\d{1,3}\s*[.、．]\s*){2,})([^\d].+)$')
RE_HIER_NUM = re.compile(
    r'^\s*(?:#{1,6}\s+)?(\d{1,3}(?:\.\d{1,3}){2,3})\s+(.+)$')

# D3 大题说明行（「综合大题（五道选两道，每题 10 分）」）：考试说明不是题干。
RE_EXAM_SECTION = re.compile(
    r'(?:大题|综合题|选做题|简答题|论述题|编程题|问答题)'
    r'.{0,12}[（(][^）)]*(?:选|任选|每题|每道|共\s*\d+\s*分|\d+\s*分)[^）)]*[）)]')
# D3 强提问语义行首：答案列表状态不能吞掉这类编号子题。
RE_STRONG_QSTART = re.compile(
    r'^(?:什么是|什么|如何|怎么|为什么|为何|哪些|哪几|谈谈|说说|简述|简答|请解释|请描述|请说明|是否|能否)')

# ---- review2 移植（2026-11-20）：问句语义/章节/答案小节词汇 ----
# 提问动词只在行首生效，避免答案句中的问词把列表项误判成题。
RE_QUESTION_INTENT = re.compile(
    r'^(?:什么是|什么叫|如何|为什么|为何|谈谈|介绍|说说|说一下|有没有|怎么办|怎么样|怎样|怎么|'
    r'简述|解释|说明|描述|列举|列出|有哪些|哪一种|哪一个|哪些|是否|能否|请|给定|假设|已知|'
    r'实现一个|设计一个|设计|计算|求出|写一个|写出|手写|阐述|证明)')
# 弱问词需后接对象才是题：裸"区别/分析/判断"是答案小节标题，不误判为新题。
RE_WEAK_INTENT = re.compile(
    r'^(?:区别|比较|分析|判断|你知道|你了解)[\u4e00-\u9fffA-Za-z0-9(（]')
# 句尾疑问形态：`linux常用吗` / `用户研究分为哪两个方面` 没有行首问词也是题。
RE_QUESTION_SUFFIX = re.compile(
    r'(?:是什么|是什么原因|有什么区别|有什么作用|有哪些内容|会怎么样|怎么回事|是否正确|如何处理|如何解决|吗|'
    r'哪[一两二三四五六七八九十0-9]+(?:个(?:方面|问题|维度)?|种|方面|类|项)|哪几个|几种|几个|多少|'
    r'是(?:（|\()\s*\d+\s*个空\s*(?:）|\)))[?？。]?$')
# `呢` 是歧义语气词：只在句中带疑问代词时才判问句（"他还没来呢"是陈述句）。
RE_NE_QUESTION = re.compile(
    r'(?:为什么|为何|怎么|如何|怎样|什么|哪[些几个]|谁|是否|能否|多少)[^。！？!]{0,24}呢[?？。]?$')
# 答案小节引导词：这类编号标题留在父答案，不另起题。
RE_LIST_INTRO = re.compile(
    r'包括|分为|有如下|如下|分别|以下|特点|优点|缺点|组成|类型|阶段|步骤|'
    r'ACID|CAP|主要有|具体如下|可以分为|适用场合|适用范围|特征')
RE_SECTION_TITLE = re.compile(
    r'^(?:第\s*[一二三四五六七八九十0-9]+\s*[章节篇]|'
    r'[一二三四五六七八九十]+[、.．]\s*)?.{1,48}'
    r'(?:面试题|基础篇|进阶篇|提高篇|专题|章节|目录|总结|概述|知识点汇总|题库)\s*[:：。．]?$')


def _strip_all_numbering(text: str) -> str:
    """去掉行首一层或多层题号/列表号，用于代码判定（不改动题干）。"""
    s = strip_bold((text or "").strip())
    while True:
        m = re.match(r'^(?:#{1,6}\s+)?(?:\d{1,3}|[一二三四五六七八九十]{1,3})\s*[.、．)）]\s*', s)
        if not m:
            break
        s = s[m.end():].strip()
    return s


def _is_codeish(text: str) -> bool:
    """行级代码判定：优先代码标点/关键字，放过“int 类型占几个字节”这类自然语言题。"""
    s = _strip_all_numbering(text)
    if not s:
        return False
    # 行尾空括号是选择题题干占位（带中文内容）；`int main （）` 这类无中文代码行不在此列。
    if re.search(r'[（(]\s*[)）]\s*$', s) and CJK.search(s):
        return False
    # 右括号+问号收尾是题干续段（`class）？`），不是代码。
    if re.search(r'[？?]\s*$', s) and re.search(r'[）)]', s):
        return False
    if s in ("{", "}", "};", "});", ");"):
        return True
    if RE_CODE_HEAD.match(s) or RE_CODE_CALL.search(s):
        return True
    if re.match(r'^\s*(?:if|for|while|switch|catch|return|else)\b', s, re.I):
        return True
    if re.match(r'^\s*而\s*[（(]', s):          # OCR 把 while 译成“而”
        return True
    if re.match(r'^\s*(?:const|static|unsigned|signed|struct|class|enum|typedef)\b', s, re.I):
        return True
    if re.match(r'^\s*(?:int|char|float|double|long|short|void|bool|size_t)\b', s, re.I):
        # 只有带代码标点/符号的声明才判代码；纯中文题干如“int 类型占几个字节”保留。
        return bool(re.search(r'[;=,\[\](){}*（）]', s)) or not RE_CJK.search(s)
    if re.match(r'^\s*(?:&&|\|\|)', s):
        return True
    if re.match(r'^\s*\((?:\s*(?:int|char|float|double|long|short|void|bool|size_t|unsigned|signed)\b)', s, re.I) and not RE_CJK.search(s):
        return True
    if re.match(r'^\s*\[[^\]]+\]\s*[?？;]?\s*$', s) and not RE_CJK.search(s):
        return True
    if re.search(r'=', s) and not _looks_questionish(s):
        return True
    if re.search(r'[\[\]]', s) and re.search(r'[（）()]', s) and not _looks_questionish(s):
        return True
    return bool(re.search(r'(?:;|\+\+|--|->|==|!=|<=|>=|\+=|-=)', s)) and not _looks_questionish(s)


def _toc_density(lines: List[str], j: int, window: int = 12) -> float:
    """j 之后 12 个非空行中“大纲形态”行的占比（F1 目录前瞻）。

    大纲形态 = 点线/页码，或短行（≤60）且不以句末标点（。！）收尾。
    问号结尾算大纲：目录条目本来就是问句（“Java 语⾔有哪些特点?”）。
    正文答案段落多带句号，密度会掉下来，以此区分目录条目与正文标题。
    """
    seen = got = 0
    for k in range(j + 1, min(len(lines), j + 400)):
        x = lines[k].strip()
        if not x:
            continue
        seen += 1
        if (RE_TOC.search(x) or RE_PAGE.match(x)
                or (len(x) <= 60 and not re.search(r"[。！]$", x))):
            got += 1
        if seen >= window:
            break
    return (got / seen) if seen else 0.0


def _toc_mask(lines: List[str]) -> set[int]:
    """识别文首目录区，返回需要标为 toc 的行号集合。

    目录经常跨行、跨页，不能只靠“点线+页码”的单行正则。这里以“目录”标题为
    起点，以第一个非目录的 H1/H2 标题（或连续页码后的首个正文行）为终点。
    """
    n = len(lines)
    start = None
    for i in range(min(n, 240)):
        # NFKC：PDF 文字层常把「目录」输出为兼容字符 ⽬录（U+2F6C）。
        if RE_TOC_HEADER.match(unicodedata.normalize("NFKC", lines[i].strip())):
            start = i
            break
    if start is None:
        return set()
    leaders = pages = outline = 0
    saw_entry = False
    end = n
    ended_at_heading = False
    # F7：预计算 H1/H2 行号索引，两处“后 5000 行内是否有 H12”的窗口扫描
    # 从 O(n·5000) 降为 O(log n)；行为与逐行 any() 完全一致。
    h12_idx = [i for i, x in enumerate(lines) if RE_MD_H12.match(x.strip())]

    def _future_h12(j: int) -> bool:
        pos = bisect.bisect_left(h12_idx, j)
        return pos < len(h12_idx) and h12_idx[pos] < j + 5000

    for j in range(start + 1, n):
        s = lines[j].strip()
        if not s:
            continue
        if RE_PAGE.match(s):
            pages += 1
            saw_entry = True
            continue
        if RE_TOC.search(s):
            leaders += 1
            saw_entry = True
            continue
        if re.fullmatch(r'[-=_—–\s]{3,}', s):
            continue
        # 目录条目多为 #### / 缩进 / 带页码；H1-H2 且不带点线，视为正文开始。
        # 无点线/页码的目录（兼容字符 ⽬录）靠短大纲条目累积证据，
        # 但必须真的遇到正文 Markdown 标题才截断，避免把全文当目录。
        # F1（金标 S0496）：目录自带 "## N." 条目时，首个 H12 可能仍是目录行
        # （075 目录 ~900 行，旧逻辑在第一条 "## 1." 就截断，其余条目裸奔成题）。
        # 前瞻 12 个非空行：多数仍是大纲形态则视为目录延续，否则才是正文标题。
        if RE_MD_H12.match(s) and not RE_TOC.search(s) and (leaders >= 2 or outline >= 3):
            if _toc_density(lines, j) >= 0.6:
                outline += 1
                saw_entry = True
                continue
            end = j
            ended_at_heading = True
            break
        if not RE_MD_H12.match(s) and len(s) <= 60 and not re.search(r'[。！？!?]$', s):
            outline += 1
        # 有目录条目、但后面没有 H1/H2 时，遇到第一个真正题目行就结束，
        # 避免把正文全部当作目录。
        if (saw_entry and not RE_TOC.search(s)
                and (_match_question(s) or _is_qline(s))
                and not _future_h12(j)):
            end = j
            break
        # 无 Markdown 标题的 PDF 目录：连续页码后，首个不缩进、非目录样式行视为正文。
        # 但若后面 5000 行内还有 H1/H2，优先等真正的正文标题，避免目录页脚提前截断。
        if (pages >= 3 and not _future_h12(j) and not s.startswith((" ", "\t"))
                and not RE_TOC.search(s) and len(CJK.findall(s)) >= 4):
            end = j
            break
    if not saw_entry and not (ended_at_heading and outline >= 3):
        # 只有“目录”标题、后面没有点线/页码/大纲条目证据：不能把全文当目录。
        return {start}
    return set(range(start, end))


def _local_outline_mask(lines: List[str], blocked: set[int]) -> set[int]:
    """后文重复出现、且首现后无答案正文的层级题目首现 → 局部目录（review2）。

    PDF 转 Markdown 常在章节开头插入局部大纲（无“目录”字样、无页码）。
    只对层级/双层编号（4.1.13 / 42.3.）启用；要求同一规范化题干在后文再次出现，
    且首现到下一道题之间没有实质答案正文，避免误删真正的无答案问题。

    三层“目录/大纲”掩码分工（同一文档三者判定口径不同，缺一会有幽灵题）：
      - _toc_mask：文首“目录”区，靠点线/页码/短大纲条目 + H12 前瞻密度；
      - _local_outline_mask（本函数）：正文中部、无“目录”字样的局部大纲，
        靠层级题号“首现 + 后文重复 + 中间无答案”；
      - answer_label / answer_subpoint_restart：答案区内重启的小节标签
        （“## 1. 共同点”“知识点/详解”），靠 qid<=last_qid 且无问句语义。
    075 实测：三者须各自覆盖；仅靠任一层都会漏（如行 273 的 H12 大纲项
    两层都不盖，行 851 的 5.2.x 条目只有本层盖住）。
    """
    occurrences: Dict[str, List[int]] = {}
    for i, line in enumerate(lines):
        if i in blocked or not line.strip():
            continue
        if not (RE_NESTED_NUM.match(line) or RE_HIER_NUM.match(line)):
            continue
        q = _match_question(line)
        if not q or not q[1].strip():
            continue
        key = _QNORM_CLEAN.sub("", q[1].lower())
        if len(key) < 6:
            continue
        occurrences.setdefault(key, []).append(i)
    out: set[int] = set()
    for indices in occurrences.values():
        if len(indices) < 2:
            continue
        for idx in indices[:-1]:
            next_q = idx + 1
            while next_q < len(lines) and not _match_question(lines[next_q]):
                next_q += 1
            if next_q >= len(lines):
                continue
            intervening = [x.strip() for x in lines[idx + 1:next_q] if x.strip()]
            has_answer = any(
                _qnorm_len(x) >= 18
                and not RE_SECTION_TITLE.match(unicodedata.normalize("NFKC", x))
                for x in intervening)
            if not has_answer:
                out.add(idx)

    # ---- P2-2：正文缺失/编号粘连的章内目录 run ----
    # key 后文再现≥2 对「编号粘连」「正文缺失」两类失效，改用区间信号：
    # 连续层级编号行之间没有实质答案正文（排除问句/粘连标题/章节标题）即为目录 run。
    def _run_answer_len(intervening: List[str]) -> int:
        total = 0
        for x in intervening:
            nfkc = unicodedata.normalize("NFKC", x)
            if RE_SECTION_TITLE.match(nfkc):
                continue
            if _match_question(x):
                continue
            if _is_qline(x) or _is_question_heading(x):
                continue
            total += _qnorm_len(x)
        return total

    RUN_MIN, ANS_BREAK, REPRO_RATIO = 8, 50, 0.5
    hier_idx = [
        i for i, line in enumerate(lines)
        if i not in blocked and line.strip()
        and (RE_NESTED_NUM.match(line) or RE_HIER_NUM.match(line))
        and _match_question(line)
    ]
    i = 0
    while i < len(hier_idx):
        run = [hier_idx[i]]
        j = i + 1
        while j < len(hier_idx):
            intervening = [
                lines[k].strip()
                for k in range(run[-1] + 1, hier_idx[j])
                if lines[k].strip()
            ]
            if _run_answer_len(intervening) >= ANS_BREAK:
                break
            run.append(hier_idx[j])
            j += 1
        if len(run) >= RUN_MIN:
            # 只有「正文缺失/编号粘连」的目录 run 才整体标 toc：
            # 正常目录（key 后文精确再现）交给上面的 key 再现逻辑，避免 span 归并。
            repro = 0
            for idx in run:
                qq = _match_question(lines[idx])
                if qq and qq[1].strip():
                    key = _QNORM_CLEAN.sub("", qq[1].lower())
                    if key in occurrences and len(occurrences[key]) >= 2:
                        repro += 1
            if repro / len(run) < REPRO_RATIO:
                for k in range(run[0], run[-1] + 1):
                    if lines[k].strip() and k not in blocked:
                        out.add(k)
        i = j
    return out


def _match_nested_number(line: str) -> Optional[Tuple[int, str]]:
    """`1. 2. 题干` / `42.3. 题干` / `11.3.1 题干` → (最后一层题号, 题干)。"""
    m = RE_NESTED_NUM.match(line)
    if m:
        nums = re.findall(r'\d{1,3}', m.group(1))
        if nums:
            return int(nums[-1]), m.group(2).strip()
    m = RE_HIER_NUM.match(line)
    if m:
        nums = re.findall(r'\d{1,3}', m.group(1))
        if nums:
            return int(nums[-1]), m.group(2).strip()
    return None


def _is_list_item_question(qtext: str) -> bool:
    """编号列表项是否带独立提问语义；不带则默认是答案列表。"""
    s = strip_bold((qtext or "").strip())
    if not s:
        return False
    if len(s) > 160:
        return True
    if s.rstrip().endswith(("：", ":")):
        return True
    if re.search(r'[?？]', s) or _is_qline(s):
        return True
    if _is_question_heading(s):
        return True
    if re.match(r'^(?:如何|怎么|为什么|什么|哪些|哪几|是否|能否|请|谈谈|说说|介绍|解释)', s):
        return True
    if re.match(r'^通过.{0,20}(?:生成|填充|输出|计算|求|实现)', s):
        return True
    return False


def _looks_section_line(s: str) -> bool:
    """面经里的“阿里二面/总监面/HR面/基础篇”等无编号章节标题。"""
    if not s or len(s) > 40 or re.search(r'[?？]', s):
        return False
    # 完整句子（带句读）是正文，不是章节标题；否则“面试官都喜欢这么问”会重置题号。
    if re.search(r'[。，；!！]', s):
        return False
    if RE_LIST_ITEM.match(s) or RE_KPOINT.match(s):
        return False
    return bool(re.search(
        r'(?:面经|面试|一面|二面|三面|四面|HR面|总监面|交叉面|基础篇|进阶篇|总结篇|'
        r'数据结构篇|算法篇|第[一二三四五六七八九十0-9]+[章部分节])', s))



def is_code_fragment(q: str) -> bool:
    """判断一行是否代码/代码碎片（只拦短行，带问句特征的不拦）。"""
    s = (q or "").strip()
    if not s or len(s) > 160:
        return False
    if re.search(r'[?？]', s) or RE_INTERROGATIVE.search(s[:12]):
        return False
    if RE_CODE_HEAD.match(s):
        return True
    if re.match(r'^\s*(?:if|for|while|switch|return)\s*\(', s):
        return True
    if RE_CODE_CALL.search(s):
        return True
    if RE_CODE_ASSIGN.match(s):
        return True
    if re.match(r'^[A-Za-z_][\w\s\*]*\([^)]*\)\s*$', s) and not RE_CJK.search(s):
        return True
    if re.search(r'[;{}]\s*$', s) and s.count(';') >= 1:
        return True
    if re.search(r'(?:\+\+|->)', s) and not re.search(r'[，。；：]', s):
        return True
    return False


def strip_bold(s: str) -> str:
    """整行粗体剥壳：`**xxx**` → xxx（含尾冒号容差）；非整行粗体返回原文。"""
    m = RE_BOLD_LINE.match(s)
    return m.group(1).strip() if m else s


def _has_question_intent(text: str) -> bool:
    """题干/任务语义：问词在行首或句尾疑问形态；英文类型名续段（class）？）不算。"""
    s = unicodedata.normalize("NFKC", strip_bold(str(text or "").strip()))
    s = re.sub(r"^[^A-Za-z0-9\u3400-\u9fff]+", "", s).strip()
    if not s:
        return False
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:[)）])?[?？]", s):
        return False
    if (RE_QEND.search(s) or RE_QUESTION_INTENT.search(s) or RE_QUESTION_SUFFIX.search(s)
            or RE_WEAK_INTENT.search(s) or RE_NE_QUESTION.search(s)):
        return True
    # 选择题空括号占位 + 题干引导词：没有问号也是题。
    if (re.search(r"[（(]\s*[）)]$", s)
            and re.match(r"^(?:以下|下列|哪(?:一|个|些|项)|哪个|哪些|何者|若|假设|已知|给定)", s)):
        return True
    return bool(re.match(r"^你(?:知道|了解|熟悉).{0,16}(?:哪些|什么|多少|如何|怎么)", s))


def _is_qline(s: str) -> bool:
    """v2 问句行判定：以？结尾，或问词出现在行首 8 字内（防"做详细介绍"类行中误命中）。
    整行粗体先剥壳（`**说说 Spring AOP**` → `说说 Spring AOP`）。"""
    s = strip_bold(s.strip())
    # 换行后的 `class）？` / `String) ?` 是上一题英文类型名的尾段，不是独立英文题。
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:[)）])?[?？]", unicodedata.normalize("NFKC", s)):
        return False
    if RE_QEND.search(s):
        return True
    # 选择题常以空括号收尾：`瀑布模型开发流程（）`
    # 但 `start()` / `run()` 这类方法调用结尾是答案正文，不是选择题空括号。
    if (re.search(r'[（(]\s*[)）]\s*$', s)
            and not re.search(r'[A-Za-z_][A-Za-z0-9_]*\s*\(\s*\)\s*$',
                              unicodedata.normalize("NFKC", s))):
        return True
    # 结论/过渡/说明句里的问词不是题干。
    if re.match(r'^(?:这就是|所以|因此|也就是说|综上|总之|那么|可见|于是|'
                r'至于|先解释|首先|其次|再次|然后|最后|先说|再说|接着|需要|'
                r'答[：:]|回答[：:]|解析[：:]|解答[：:])', s):
        return False
    if re.match(r'^(?:的|了|是|在|和|与|及|并|而|但|就|也|都|其|此|该)\s', s):
        return False
    # `Redis 是内存数据库` 是陈述句；`ReadWriteLock 是什么` 仍是问句。
    if re.match(r'^[A-Za-z][A-Za-z0-9_\s]*是(?!什么|否)', s):
        return False
    return (RE_INTERROGATIVE.search(s[:8]) is not None
            or RE_QUESTION_SUFFIX.search(s) is not None
            or RE_NE_QUESTION.search(s) is not None)


def _is_strong_qline(s: str) -> bool:
    """强问句信号：行首强问词（RE_STRONG_QSTART）+ 问号收尾（RE_QEND）。

    P1 去掉 prev_blank 硬依赖后，用它替代过宽的 _is_qline 作为"无需空行前置
    即可独立成题"的判定。比 _is_qline 严格（是其子集）：答案正文里的设问句
    （"这个功能常用吗？"行首"这个"非强问词）不会被误拆，而"什么是最左前缀
    原则？"这类行首问词+问号收尾的真题能接住（金标 E_ANSWER_BLEED 漏切）。
    """
    s = strip_bold(s.strip())
    return bool(RE_QEND.search(s) and RE_STRONG_QSTART.match(s))


RE_EXPLANATION_TAIL = re.compile(r'(?:下图便是|如下图所示|我建议|我们来看|具体实现如下)')


def _strip_explanation_tail(s: str) -> str:
    """任务型题干（无问号）后粘连的说明句截断，只切说明连接词、不碰问号。

    PDF 常把节标题与首句粘在同一行（金标 S0537 型“说⼀下Java对象的创建
    过程下图便是 Java 对象的创建过程…”）。题号行的 qtext 可能是多行题干
    的首行（D1 回接前），故此处不能做问号截断，否则误伤续行题干。
    """
    s = s.strip()
    if "?" not in s and "？" not in s:
        cut = RE_EXPLANATION_TAIL.search(s)
        if cut and cut.start() >= 4:
            return s[:cut.start()].rstrip()
    return s


def _trim_question_text(s: str) -> str:
    """一行里“问句 + 紧跟的答案说明”只保留问句部分。"""
    s = s.strip()
    m = re.match(r'^(.{1,120}?[?？])\s*(.{4,})$', s)
    if m:
        return m.group(1)
    return _strip_explanation_tail(s)


# D1 题干续行回接（docs/37 line_wrap_split）：题干以连接词/介词悬空结尾，
# 下一内容行是问句形态的短行 → 续回题干，不另成题、不并入答案。
RE_STEM_DANGLING = re.compile(r'[的和与或及中对把被将在是为而但则之其该等地得所并跟向从到过着了，、:：]$')
RE_ANSWER_LEAD = re.compile(r'^(?:答案|答[：:]|解[：:]|解析|参考|示例|代码|注意|说明|综上|总之)')


def _is_stem_continuation(prev_q: str, line: str) -> bool:
    """line 是否为悬空题干 prev_q 的换行续段（保守）。

    问句形态短行 + 非问句形态续段（数学式/中文半句被换行、空行拆断）都认；
    答案引导、题号、代码、章节标题、列表项开路时不认，防止把真实答案误并回题干。
    """
    s = strip_bold((line or "").strip())
    if not s or len(s) > 60 or not prev_q:
        return False
    if (RE_LIST_ITEM.match(s) or RE_DECIMAL.match(s) or RE_ANSWER_LEAD.match(s)
            or _match_question(line) is not None
            or _is_codeish(s) or _is_promo(s) or _looks_section_line(s)):
        return False
    prev_done = bool(re.search(r'[。！？?!]$', prev_q.rstrip()))
    # P1-3(b)：题干未封口时，续段以虚词开头（「…多进制」+「的计算机，为什么」）
    # 是上一句被换行拆开的强信号；独立问句/答案句几乎不以虚词开头。
    if (not prev_done
            and re.match(r'^(?:的|了|是|在|和|与|及|并|而|但|就|也|都|其|此|该)(?=\S)', s)):
        return True
    if _is_qline(s) or _is_question_heading(s):
        # 原判据：prev 以连接词/介词悬空结尾。
        if RE_STEM_DANGLING.search(prev_q.rstrip()):
            return True
        # P1-1 兜底：prev 以实词（动词等）结尾时，紧随的极短问句行仍是题干续行
        # （S0129 "…最后结束" + "的线程?"）。用 ≤15 字 + 问号收尾双条件收窄，
        # 避免把答案里的短问句误并回题干。
        if (len(s) <= 15 and re.search(r'[?？]\s*$', s)
                and not RE_QUESTION_INTENT.match(s)):
            return True
        return False
    # P1-3(a)/(c)：非问句形态续段（金标 E_STEM_SPLIT：数学式、中文半句换行）。
    if prev_done:
        return False
    # (a) 题干以「证明/求/计算」等任务词+冒号收尾 → 其后命题/公式是题干。
    if (prev_q.rstrip().endswith(('：', ':'))
            and re.search(r'(?:证明|求证|求|计算|求解|写出|推导|化简|等于|结果|输出)', prev_q)):
        return True
    # (c) 题干以助词/连接词悬空 + 续段极短（半个词/短片段）。
    return bool(RE_STEM_DANGLING.search(prev_q.rstrip())) and len(s) <= 15


def _join_stem(a: str, b: str) -> str:
    if a and b and a[-1].isascii() and a[-1].isalnum() and b[0].isascii() and b[0].isalnum():
        return a + " " + b
    return a + b


def _looks_questionish(s: str) -> bool:
    """推广过滤用：像题目/问句的行不按广告剔除（不要求空行等块级条件）。"""
    s = strip_bold(s.strip())
    return bool(RE_QEND.search(s) or RE_INTERROGATIVE.search(s[:8]) or RE_KPOINT.match(s))


def _is_question_heading(s: str) -> bool:
    """章节/加粗小标题是否具备题目语义。

    `_is_qline` 只查行首 8 字，像“sleep方法和wait方法有什么区别”这种
    问词在句中的标题会漏；这里补一层“标题型问句”判定，但仍排除
    “知识点汇总/本地方法栈/简化配置”这类答案小节。
    """
    s = strip_bold((s or "").strip())
    if not s:
        return False
    # 列表/对比引导句先排除：“两者的主要区别如下：”虽含问词，但不是题目。
    if s.endswith(("：", ":")) and re.search(r'(?:如下|以下|包括|几点|几个方面)', s):
        return False
    if re.match(r'^(?:两者|二者|下面|下列|主要|具体来看|简单来说|也就是说|需要注意)', s):
        return False
    # 答案里的枚举/说明句，不是标题：“首先说清楚什么是缓存”。
    if re.match(r'^(?:首先|其次|再次|然后|最后|先说|再说|接着|至于|先解释)', s):
        return False
    if re.match(r'^(?:的|了|是|在|和|与|及|并|而|但|就|也|都|其|此|该)\s', s):
        return False
    if re.match(r'^[A-Za-z][A-Za-z0-9_\s]*是(?!什么|否)', s):
        return False
    if _is_qline(s) or RE_KPOINT.match(s):
        return True
    # 编号标题里问号在句中（`接口是否可继承（extends）接口？抽象类是否可实现`）也是题。
    if re.search(r'[?？]', s) and len(s) <= 160:
        return True
    if len(s) > 60:
        return False
    if re.match(r'^(?:简单总结|总结一下|简单来说|也就是说|需要注意)', s):
        return False
    if s.endswith("。") and not re.search(r'[?？]', s):
        return (len(s) <= 35 and bool(re.search(
            r'(?:如何|怎么|为什么|为何|是什么|什么是|哪些|哪几|区别|流程|机制|作用|原理|实现方式)',
            s)))
    return bool(re.search(
        r'(?:是什么|什么是|如何|怎么|为什么|为什么|为何|哪些|哪几|区别|作用|原理|流程|'
        r'机制|优缺点|优点|缺点|使用场景|实现方式)', s))


def _heading_questionish(qtext: str) -> bool:
    """F5（金标 S0517/S0533）：Markdown 编号标题成题的最低语义门槛。

    编号标题（`## N. xxx`）没有统一问句形态：真题干可能是问句、任务句
    或短名词短语（030 型 `## 11. Oracle JDK 和 OpenJDK 的对⽐`）；但
    “熟悉/了解/掌握”开头的技能罗列、“…为了让⼤家更清楚…”这类节头+
    说明句也是编号标题，被当题会带来答案 bleed。成题至少要有：问句/
    任务语义，或不含句读的短名词短语。
    """
    s = unicodedata.normalize("NFKC", qtext or "").strip()
    if not s:
        return False
    # 技能罗列列表项（“熟悉如何优化 Java 代码…”）是简历/学习建议，不是题。
    if re.match(r"^(?:熟悉|了解|掌握|精通|具备)", s) and not s.rstrip().endswith(("?", "？")):
        return False
    if (RE_QEND.search(s) or _has_question_intent(s)
            or RE_QUESTION_SUFFIX.search(s) or _is_question_heading(s)):
        return True
    # 短名词短语标题（“Oracle JDK 和 OpenJDK 的对⽐”）按题处理；过短的
    # （“队列”，qnorm<3）是答案小节，带句读/说明句形态的（“招聘⼈数 ：秋招
    # 多于春招 ；”）不是题。
    return _qnorm_len(s) >= 3 and len(s) <= 40 and not re.search(r"[，,；;。.！!]", s)


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
    # 网页/PDF 标题常把“参考：URL”和下一节层级编号拼在一行：
    # `参考：https://... 11.3.0 如何查看...` / `...图11.1.7 什么是 mongod？`
    # 先去掉 URL，再取行内层级编号之后的题干，避免引用和编号进入题干。
    line = re.sub(r'https?://\S+', ' ', line)
    # 行内出现“11.1.7 什么是…”这类层级编号：取最后一个编号后的题干。
    # 典型来源是网页/PDF 把“参考：URL”或上一节标题和下一节题目拼在同一行。
    for hm in reversed(list(re.finditer(r'\d{1,3}(?:\.\d{1,3}){2,3}', line))):
        if hm.start() <= 0:
            continue
        body = line[hm.end():].strip()
        if (len(body) >= 4 and not _is_codeish(body)
                and not body.startswith(("的", "了", "是", "在", "和", "与"))
                and (_is_question_heading(body)
                     or re.match(r'^(?:什么|如何|怎么|为什么|为何|哪些|哪几|请|简述|说说|介绍|解释|描述|实现|编写|设计|给定|求)', body))):
            return None, body, "标题"
    nested = _match_nested_number(line)
    if nested and (re.search(r'[?？]', nested[1]) or len(nested[1]) >= 4):
        return nested[0], nested[1], "题号"
    if RE_DECIMAL.match(line):
        return None
    # 中文逗号题号：`54，jsp静态包含和动态包含的区别`
    m = re.match(r'^\s*(\d{1,3})\s*[，,]\s*(.+)$', line)
    if m:
        body = m.group(2).strip()
        if re.search(r'[?？]', body) or _is_question_heading(body):
            return int(m.group(1)), body, "题号"
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


RE_INLINE_HIER = re.compile(r'(?<![\w./])(\d{1,4}(?:[.、．]\d{1,3}){2,})[ \t]+')


def _split_inline_hierarchical_lines(md_text: str) -> Tuple[List[str], List[int]]:
    """把正文行内粘连的层级题号拆成虚拟行，同时保留原文字符偏移（review2）。

    `使用命令 ... 11.3.1 什么是复制？` 中命令属于前一答案，`11.3.1` 才是新题起点。
    拆分不改动 md_text：虚拟行的 starts 仍指向原文，source_span/a_segs 可精确回溯。
    """
    source_lines = md_text.splitlines(keepends=True)
    lines: List[str] = []
    starts: List[int] = [0]
    pos = 0
    for raw in source_lines:
        split_at = None
        plain = raw.strip()
        if plain and not plain.startswith(("#", "```", "~~~", ">")):
            for m in RE_INLINE_HIER.finditer(plain):
                suffix = plain[m.end():].strip()
                prefix = plain[:m.start()].strip()
                if (prefix and len(suffix) >= 4 and not _is_codeish(suffix)
                        and not suffix.startswith(("的", "了", "是", "在", "和", "与"))
                        and (prefix[-1] in "）)】」』。！？!?；;：:"
                             or _is_question_heading(suffix)
                             or re.match(r'^(?:什么|如何|怎么|为什么|为何|哪些|哪几|请|简述|说说|介绍|解释|描述|实现|编写|设计|给定|求)', suffix))):
                    split_at = raw.find(plain) + m.start()
                    break
        end = pos + len(raw)
        if split_at is None:
            lines.append(raw)
            starts.append(end)
        else:
            prefix, suffix = raw[:split_at], raw[split_at:]
            if prefix:
                lines.append(prefix)
                starts.append(pos + len(prefix))
            if suffix:
                lines.append(suffix)
                starts.append(end)
        pos = end
    return lines, starts[:len(lines) + 1]


def _reclaim_stem_continuation(q_text: str, a_text: str, a_segs: list | None):
    """将因 Markdown/OCR 换行落入答案区的题干续行收回题目（review2）。

    仅处理强续接证据：题干末尾未完成，且下一非空答案行以括号内容或代码标识符
    续接（`（implements）` / `class）？`）。普通答案句、答案标签和新题标题不消费；
    a_segs 同步裁剪，保留原文偏移。
    """
    q = str(q_text or "").rstrip()
    if not q or not a_text:
        return q_text, a_text, a_segs
    if re.search(r"[。！？?!；;:]$", q):
        return q_text, a_text, a_segs
    lines = a_text.splitlines(keepends=True)
    if not lines:
        return q_text, a_text, a_segs
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return q_text, a_text, a_segs
    first = lines[i].strip()
    if re.match(r"^(?:答(?:案)?[：:]|解析[：:]|A[：:]|答案如下)", first, re.I):
        return q_text, a_text, a_segs
    q_nfkc = unicodedata.normalize("NFKC", q)
    first_nfkc = unicodedata.normalize("NFKC", first)
    open_parens = q_nfkc.count("(") - q_nfkc.count(")")
    strong_start = bool(re.match(
        r"^(?:[（(](?:implements|extends|concrete|abstract|interface|class|void|int|char|String|[A-Za-z_][\w .<>]*)[）)]"
        r"|[A-Za-z_][\w]*[）)]|class[）)])", first_nfkc, re.I))
    incomplete_suffix = bool(re.search(
        r"(?:是否可|是否能|是否会|可实现|可继承|具体类\s*[（(][^）)]*|接口\s*[（(][^）)]*)$", q_nfkc))
    if not (strong_start and (open_parens > 0 or incomplete_suffix
                              or not re.search(r"[。！？?!]$", q))):
        return q_text, a_text, a_segs
    consumed = i
    parts = [q.strip()]
    max_lines = 3
    while consumed < len(lines) and max_lines > 0:
        piece = lines[consumed].strip()
        if not piece:
            # OCR 常在英文类型名中间插空行（`(concrete` ↔ `class)？`）；
            # 仅当下一非空行明确补齐标识符/括号时才跨过。
            look = consumed + 1
            while look < len(lines) and not lines[look].strip():
                look += 1
            next_piece = unicodedata.normalize("NFKC", lines[look].strip()) if look < len(lines) else ""
            if (next_piece
                    and re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_]*[)）][?？]?|[)）][?？]?)", next_piece)):
                consumed += 1
                continue
            if consumed == i:
                consumed += 1
                continue
            break
        if re.match(r"^(?:答(?:案)?[：:]|解析[：:]|\d{1,4}[.、．]|#{1,6}\s)", piece, re.I):
            break
        if _is_codeish(piece):
            break
        parts.append(piece)
        consumed += 1
        max_lines -= 1
        joined = " ".join(parts)
        normalized = unicodedata.normalize("NFKC", joined)
        if (re.search(r"[？?。]$", joined)
                and normalized.count("(") <= normalized.count(")")):
            break
        if consumed < len(lines):
            nxt = lines[consumed].strip()
            if nxt and re.match(r"^(?:答(?:案)?[：:]|解析[：:]|#{1,6}\s|\d{1,4}[.、．])", nxt):
                break
            nxt_nfkc = unicodedata.normalize("NFKC", nxt)
            if nxt and not (re.match(r"^(?:[）)]|[A-Za-z_][\w]*[）)]|class[）)])", nxt_nfkc, re.I)
                            or re.match(r"^[（(][A-Za-z_][\w .<>]*[）)]", nxt_nfkc)):
                break
    if len(parts) == 1:
        return q_text, a_text, a_segs
    remainder_start = consumed
    while remainder_start < len(lines) and not lines[remainder_start].strip():
        remainder_start += 1
    new_answer = "".join(lines[remainder_start:])
    new_segs = list(a_segs[remainder_start:]) if a_segs else a_segs
    return " ".join(parts), new_answer, new_segs


def chunk(md_text: str, *, split_long: bool = True) -> Dict:
    lines, starts = _split_inline_hierarchical_lines(md_text)
    n = len(lines)
    total = starts[n]

    # ---- 第一遍：行分类 ----
    kinds: List[str] = ["text"] * n
    toc_lines = _toc_mask(lines)
    toc_lines |= _local_outline_mask(lines, toc_lines)
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
        if i in toc_lines:
            kinds[i] = "toc"
            continue
        if s.startswith("|"):
            kinds[i] = "table"
            continue
        if s.startswith(">"):
            kinds[i] = "quote"
            continue
        if _is_codeish(s):
            kinds[i] = "code"
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
        if re.match(r'^#\s+\S', ln) and not RE_MD_NUM_HEADING.match(s):
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
    hier_section_lines: set = set()   # F3b：层级节标题硬边界行（组装时收紧 end_line）
    last_qid: Optional[int] = None
    prev_blank = True
    last_problem_mark = False   # v2 增强：问题N 纯标记行武装紧随的独立问句行
    list_active = False         # 普通编号列表：1. 原子性 / 4. 持久性 不是题
    list_pending = False        # 上一行是“如下/以下/四个要素”等列表引导
    last_hier_was_section = False   # F3：上一个层级行是否被判为章节（章节运行上下文）
    for i, ln in enumerate(lines):
        if kinds[i] != "text" or ln.strip() in watermark:
            # 页码/目录/页眉/水印只是"扣除区"，不能消费块级空行；否则 `12\n3. 下一题`
            # 会把空行前置条件清掉，导致下一题被并进上一题。
            if not ln.strip():
                prev_blank = True
            elif kinds[i] in ("doc_header", "section"):
                prev_blank = True     # 标题/文档头/章节也是块边界，下一行可新开题
            elif kinds[i] == "hier_section":
                # F3b 层级节标题：不武装 prev_blank——紧随的顺延题号行
                # （“1.1.6 如果需要笔试…”）不是新题，避免备战面试散行成题。
                pass
            elif kinds[i] == "code":
                # 代码区不重置题号：同一节里“函数代码 + 下一题”仍应连续可识别。
                pass
            last_problem_mark = False
            continue
        s = ln.strip()
        if not s:
            prev_blank = True
            last_problem_mark = False
            # 空行后如果接的是列表项，说明列表还在继续；
            # 上一行没有句末标点（跨行断句）时也视为列表续行。
            j = i + 1
            while j < n and not lines[j].strip():
                j += 1
            nxt = lines[j].strip() if j < n else ""
            prev_open = bool(lines[i - 1].strip() and not lines[i - 1].strip().endswith(
                ("。", "！", "？", "；", ".", "!", "?", ";", "）", ")")))
            nxt_is_list = bool(nxt and RE_LIST_ITEM.match(nxt) and not nxt.startswith("#"))
            nxt_is_q = bool(nxt and (_is_qline(nxt) or _is_question_heading(nxt)))
            if nxt_is_q:
                prev_open = False
            if not (nxt_is_list or prev_open):
                list_active = False
            continue
        # D3 大题说明行：重置题号上下文，整行不产题（带问号收尾的真题目除外）
        if RE_EXAM_SECTION.search(s) and not re.search(r'[?？]\s*$', s):
            last_qid = None
            list_active = False
            list_pending = False
            prev_blank = True
            last_problem_mark = False
            continue
        # F3（金标 S0537/S0549 E_ANSWER_BLEED）：PDF 行粘连的层级节标题
        # （“2.4.2 说⼀下Java对象的创建过程下图便是…”）。层级号 ≥2 段不会出现在
        # 答案内部列举（答案用 1. 2. / ①②③），因此按边界处理：有问句语义 → 成题；
        # 否则章节硬边界（内容归 leftovers）。若不拦截，_match_nested_number 会给出
        # (末段号, 题文, 题号)，prev_blank=False 时整节内容被吸入上一题答案。
        # 顺延例外：题区里的顺延层级行（“2.2.4 HashMap 和 HashSet区别…”）沿用旧
        # 路径成题；但若上一个层级行已被判为章节（备战面试建议块 1.1.5→1.1.9），
        # 章节运行中的顺延兄弟行同样按章节处理，避免建议散行成题。
        hm = RE_HIER_NUM.match(s)
        hier_tail = int(hm.group(1).split(".")[-1]) if hm else None
        sequential_hier = (prev_blank and hier_tail is not None
                           and last_qid is not None and hier_tail == last_qid + 1)
        if (hm and len(hm.group(2).strip()) >= 4
                and not _has_question_intent(hm.group(2).strip())
                and not [m for m in re.finditer(r"\d{1,3}(?:\.\d{1,3}){2,}", s)
                         if m.start() > 0]
                and not (last_qid is None
                         or (sequential_hier and not last_hier_was_section))):
            body = hm.group(2).strip()
            # ponytail: 枚举型短标题（“HTTP⻓连接,短连接”）按题处理；若未来
            # 出现误判（逗号开头的非题短行），再升级为动词开头黑名单。
            if ((_is_question_heading(body) and len(body) <= 60
                    and not re.search(r"[。！；]", body))
                    or (len(body) <= 40 and re.search(r"[，,、]", body)
                        and not re.search(r"[。！？；]", body)
                        and not body.endswith(("：", ":")))):
                # 标题语义（“HashMap 的底层实现原理”）：成题；last_qid 不动，
                # 保持旧的顺延跟踪，避免级联（后续 qid==1 行被误判新题）。
                # 整句（含 。！；）不是标题，如“搞清楚…哪些⽅向的问题吧！”。
                bounds.append({"line": i, "parts": [(None, body)], "style": "标题"})
                last_hier_was_section = False
            else:
                # 章节说明（“说⼀下Java对象的创建过程下图便是…”）：硬边界，
                # 行与后续正文归 leftovers，不再吸入上一题答案。用独立 kind
                # 而非 "section"：不武装 prev_blank（见上文行类型循环）。
                kinds[i] = "hier_section"
                hier_section_lines.add(i)
                last_hier_was_section = True
            list_active = False
            list_pending = False
            prev_blank = False
            last_problem_mark = False
            continue
        if hm:
            last_hier_was_section = False   # 走旧路径的层级行：题区上下文
        q = _match_question(ln)
        if q is None:
            # OCR/网页题库常见的“题号独占一行”：`1.` 下一行才是题干。
            num_only = re.fullmatch(r'\d{1,3}[.、．]', s)
            if num_only:
                j = i + 1
                while j < n and not lines[j].strip():
                    j += 1
                if j < n:
                    nxt = lines[j].strip()
                    if _is_promo(nxt):
                        j += 1
                        while j < n and not lines[j].strip():
                            j += 1
                        nxt = lines[j].strip() if j < n else ""
                    if (len(nxt) >= 4 and not _is_codeish(nxt)
                            and not _match_question(nxt)
                            and not RE_LIST_ITEM.match(nxt)):
                        qid_only = int(re.match(r'\d{1,3}', s).group())
                        bounds.append({"line": j, "parts": [(qid_only, nxt)], "style": "题号"})
                        last_qid = qid_only
                        prev_blank = False
                        list_active = False
                        list_pending = False
                        last_problem_mark = False
                        continue
            # F4a（金标 075 分⻚/分段）：PDF 粘连的“节标题+面试官问句”整行，
            # 题号被 RE_DECIMAL 吞掉后真问题随行丢失/被吸入上一题答案。
            # 剥出面试官问句部分成题，节标题前缀丢弃。
            m_iv = re.search(r'(?:⾯试官|面试官|追问|继续问|接着问)\s*[：:]\s*(.+)$', s)
            if m_iv and _is_qline(m_iv.group(1).strip()):
                bounds.append({"line": i,
                               "parts": [(None, _trim_question_text(m_iv.group(1).strip()))],
                               "style": "问句"})
                list_active = False
                list_pending = False
                prev_blank = False
                last_problem_mark = False
                continue
            # 无编号章节标题（面经里的“阿里二面/HR面”）重置题号上下文：
            # 下一节的 1. 虚函数/2. ... 不能被上一节的 20 判成列表。
            if _looks_section_line(s):
                last_qid = None
                list_active = False
                list_pending = False
                prev_blank = True
                last_problem_mark = False
                continue
            # 列表引导行（“如下/以下/包括/四个基本要素/BATJ 的 4 个点”）
            if len(s) <= 160 and RE_LIST_LEAD.search(s) and not RE_LIST_ITEM.match(s):
                list_pending = True
            # 列表项可能跨行（“过 / 废的方法。”），非编号续行不能结束列表状态。
            # 真正的题边界会在下方 new_q 分支重置 list_active。
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
            standalone = (7 < len(inner) <= 160
                    and not RE_LIST_ITEM.match(inner) and not RE_DECIMAL.match(inner)
                    and not inner.rstrip().endswith(("：", ":"))
                    and not _is_codeish(inner))
            if standalone and (
                    ((prev_blank or last_problem_mark) and _is_qline(inner))
                    or _is_strong_qline(inner)):
                bounds.append({"line": i, "parts": [(None, _trim_question_text(inner))], "style": "问句"})
                list_pending = False
                list_active = False
            last_problem_mark = False
            prev_blank = False
            continue
        qid, qtext, style = q
        qtext = re.sub(r'^\d{1,3}\s*[，,]\s*', '', qtext)
        qtext = qtext.replace("**", "").strip().lstrip("、，,。；;：:")
        qtext = _strip_explanation_tail(qtext)
        # F5c（金标 028 S0348）：叙述行“1、上来照例还是问了问项目：”
        # 有题号但整行是面经叙事，不是题；成题会偷走后面的叙述内容。
        # 结尾标点是面经叙述的常见收束（：/；），两者都按叙述行拦。
        if (style == "题号" and RE_NARRATIVE_LEAD.search(qtext)
                and qtext.rstrip().endswith(("：", ":", "；", ";"))
                and not re.search(r"[?？]", qtext)):
            kinds[i] = "section"
            last_qid = None
            list_active = False
            list_pending = False
            prev_blank = True
            last_problem_mark = False
            continue
        if style == "标题":
            # 章节标题（“MyBatis 面试题”“Java 并发编程（一）”）不是题；只有带
            # 提问语义的标题（“什么是 JVM？”）才作为标题题块。
            if (_is_codeish(qtext)
                    or re.match(r'^(?:有一点|注意|总结|如下|以下|主要|首先|其次|最后)', qtext)
                    or not _is_question_heading(qtext)):
                # review2：真章节标题是硬边界——结束当前答案、重置题号，
                # 并进 leftovers(章节标题) 参与 coverage 扣除；答案小节标题仍留答案区。
                if (s.startswith("# ") or _looks_section_line(qtext)
                        or "面试题" in qtext or RE_SECTION_TITLE.match(qtext)):
                    kinds[i] = "section"
                    last_qid = None
                list_active = False
                list_pending = False
                prev_blank = True
                last_problem_mark = False
                continue
            bounds.append({"line": i, "parts": [(None, qtext)], "style": "标题"})
            last_qid = qid if qid is not None else 0
            list_active = False
            list_pending = False
            prev_blank = True
            last_problem_mark = False
            continue
        # 普通编号列表（ACID 的 1./2./3./4.）在“列表引导”后不是题。
        # 另一个来源：上一题后面紧跟的无标题编号行（答案里的 1、2、3、…），
        # 若没有提问语义，默认是答案列表；显式 Markdown 编号标题不受此限。
        md_num_heading = bool(RE_MD_NUM_HEADING.match(s))
        bold_only_heading = bool(re.match(r'^\s*\*\*', s) and re.search(r'\*\*\s*$', s))
        chinese_num_item = bool(re.match(r'^\s*[一二三四五六七八九十]+[、.．]', s))
        if (chinese_num_item and not _is_question_heading(qtext)
                and len(qtext.strip()) <= 20):
            list_active = False
            list_pending = False
            prev_blank = False
            last_problem_mark = False
            continue
        # Markdown 编号标题/加粗小标题如果是“知识点汇总/本地方法栈”这类答案小节，
        # 不是新题。显式编号且题号连续时仍按题处理（030 这类整篇 ## N、题）；
        # 非连续编号只有带提问语义才作为题边界（073 的 q11/q12）。
        answer_label = bool(re.match(
            r'^(?:知识点|总结|汇总|详解|概述|简介|本地方法栈|程序计数器|方法区|虚拟机栈|堆|栈)$',
            qtext.strip()))
        # review2：明确问句/任务语义优先于题号连续性（跨章节跳号不再吞题）。
        explicit_question = _has_question_intent(qtext)
        # F4b（金标 075 分⻚/分段）：qnorm<6 且冒号结尾的顺延编号标题
        # （“## 2. 区别 ：”）是答案小节标签，不是题——留在父题答案，否则
        # 碎片题偷走答案后半。_is_question_heading 会因“区别”关键词放行，
        # 故在 new_q 之前拦。无冒号的短标题（“下一题”）不受影响。
        if (md_num_heading and _qnorm_len(qtext) < 6
                and qtext.rstrip().endswith(("：", ":"))
                and not explicit_question):
            list_active = False
            list_pending = False
            prev_blank = False
            last_problem_mark = False
            continue
        # 显式 Markdown 编号题头（`## 11. Oracle JDK 和 OpenJDK 的对比`）即使没有
        # 问词也是题；答案小节/章节标题除外。答案要点会在父题下重启编号
        # （`## 1、只有一个文件 dump.rdb…` 紧跟 `## 38、…？`），qid<=last_qid
        # 且无问句/任务语义的是答案小节，不是新题；首个题头或顺延编号才按题头处理。
        answer_subpoint_restart = (
            last_qid is not None and qid is not None
            and qid <= last_qid and not explicit_question)
        # F5（金标 S0517/S0533）：Markdown 编号标题成题需要最低问句/标题
        # 语义（_heading_questionish）。强语义（问号/问词/标题型问句）直接
        # 成题（旧行为）；短名词短语只在非答案小节语境成题；无语义的短行
        # 留父题答案（旧行为）；节头+说明句（“## 6. ⼀个简单的线程池Demo…
        # 为了让⼤家更清楚…”）是章节硬边界，行归 leftovers，防答案 bleed。
        if md_num_heading:
            # 编号标题行无论去向（成题/吸收进父题答案/章节硬边界）都单调推进
            # last_qid：下游 answer_subpoint_restart 与 F3b 顺延守卫都依赖它，
            # 不推进会让后续层级行/编号行逃逸成题（T5 实测）。粘连整行
            # （“## 1. 编程式…2. 声明式…”）推进到行内最大题号——HEAD 中
            # 这些 part 是独立题、会把 last_qid 推到最大值，吸收后须等价。
            adv = qid
            for m in RE_GLUED.finditer(qtext):
                if int(m.group(1)) > adv:
                    adv = int(m.group(1))
            if adv is not None and (last_qid is None or adv > last_qid):
                last_qid = adv
            heading_q = _heading_questionish(qtext)
            strong_q = bool(heading_q and (
                    RE_QEND.search(qtext) or _has_question_intent(qtext)
                    or RE_QUESTION_SUFFIX.search(qtext)
                    or _is_question_heading(qtext)))
            if not heading_q:
                # R9 粘连整行（“junit 用法…2. 分布式锁”）要到 new_q 才拆分；
                # 拆分后任一部分有语义，整行仍按题走。
                parts, cur_qid, seg_start = [], qid, 0
                for m in RE_GLUED.finditer(qtext):
                    num = int(m.group(1))
                    if num == cur_qid + 1:
                        parts.append(qtext[seg_start:m.start(1)].rstrip())
                        cur_qid, seg_start = num, m.start(1)
                parts.append(qtext[seg_start:].rstrip())
                # R9 尾部 part 保留题号前缀（“2. 分布式锁”），判定前剥掉。
                heading_q = any(_heading_questionish(
                    re.sub(r'^\d{1,3}\s*[.、．]\s*', '', p)) for p in parts)
            if heading_q and (strong_q
                    or (not answer_subpoint_restart and not answer_label
                        and not RE_SECTION_TITLE.match(qtext.strip())
                        and not RE_LIST_INTRO.search(qtext.strip()))):
                pass  # 落 new_q（旧行为）
            elif (answer_subpoint_restart or answer_label
                    or RE_SECTION_TITLE.match(qtext.strip())
                    or RE_LIST_INTRO.search(qtext.strip())
                    or len(qtext.strip()) <= 40):
                list_active = False
                list_pending = False
                prev_blank = False
                last_problem_mark = False
                continue
            else:
                # 章节硬边界；不动 last_qid/prev_blank——T5 实测重置会绕过
                # F3b 的 last_qid 守卫，让后续层级行级联成题。
                kinds[i] = "hier_section"
                hier_section_lines.add(i)
                list_active = False
                list_pending = False
                prev_blank = False
                last_problem_mark = False
                continue
        if bold_only_heading and not md_num_heading and not _is_question_heading(qtext):
            list_active = False
            list_pending = False
            prev_blank = False
            last_problem_mark = False
            continue
        implicit_list = (last_qid is not None and prev_blank and not md_num_heading
                         and (qid <= last_qid or last_qid == 0)
                         and not _is_list_item_question(qtext))
        # 已进入编号列表：后续编号行默认是答案列表项；
        # 只有明确以问号收尾（“2. 什么是…？”）才保留为题。
        # D3 例外：空行前置 + 强提问语义行首的编号子题不被列表状态吞掉。
        strong_subq = (prev_blank and RE_STRONG_QSTART.match(qtext)
                       and not qtext.rstrip().endswith(("：", ":"))
                       and _qnorm_len(qtext) >= 6)
        if (list_active and RE_LIST_ITEM.match(s) and not md_num_heading
                and not qtext.rstrip().endswith(("?", "？")) and not strong_subq
                and not explicit_question):
            list_active = True
            list_pending = False
            prev_blank = False
            last_problem_mark = False
            continue
        if (not md_num_heading and (list_active or list_pending or implicit_list)
                and not explicit_question
                and not _is_list_item_question(qtext)):
            list_active = True
            list_pending = False
            prev_blank = False
            last_problem_mark = False
            continue
        list_pending = False
        list_active = False
        new_q = False
        if RE_PROBLEM_N.match(s):
            new_q = True          # 问题N：作者显式标记的题号，不依赖空行/递进口径
        elif md_num_heading:
            new_q = True          # 显式 Markdown 编号标题：无条件新题边界
        elif explicit_question:
            new_q = True          # review2：明确问句/任务语义优先于题号连续性
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
            parts, cur_qid, seg_start = [], qid, 0
            for m in RE_GLUED.finditer(qtext):
                num = int(m.group(1))
                if num == cur_qid + 1:
                    parts.append((cur_qid, qtext[seg_start:m.start(1)].rstrip()))
                    cur_qid, seg_start = num, m.start(1)
            parts.append((cur_qid, qtext[seg_start:].rstrip()))
            bounds.append({"line": i, "parts": parts, "style": "题号"})
            last_qid = parts[-1][0]
        prev_blank = False
        last_problem_mark = False

    # ---- D1 题干续行回接：悬空题干 + 问句形态短续行 → 并入题干 ----
    bound_lines = {bd["line"] for bd in bounds}
    consumed: set[int] = set()

    def _is_noise(j: int) -> bool:
        s = lines[j].strip()
        return (not s or kinds[j] in ("page", "toc", "ack")
                or (kinds[j] == "text" and s in watermark) or j in consumed)

    def _next_content(i: int) -> int:
        j = i + 1
        while j < n and _is_noise(j):
            j += 1
        return j

    rejoined: List[dict] = []
    bi = 0
    while bi < len(bounds):
        bd = bounds[bi]
        parts = list(bd["parts"])
        last_line = bd["line"]
        while True:
            j = _next_content(last_line)
            if j >= n or kinds[j] != "text":
                break
            if j in bound_lines:
                nb = bounds[bi + 1] if bi + 1 < len(bounds) else None
                if (nb is not None and nb["line"] == j and nb["style"] in ("问句", "标题")
                        and len(nb["parts"]) == 1
                        and all(_is_noise(k) for k in range(last_line + 1, j))
                        and _is_stem_continuation(parts[-1][1], lines[j])):
                    parts[-1] = (parts[-1][0], _join_stem(parts[-1][1], nb["parts"][0][1]))
                    consumed.add(j)
                    last_line = j
                    bi += 1
                    continue
                break
            if not _is_stem_continuation(parts[-1][1], lines[j]):
                break
            parts[-1] = (parts[-1][0], _join_stem(parts[-1][1], strip_bold(lines[j].strip())))
            consumed.add(j)
            last_line = j
        rejoined.append({"line": bd["line"], "parts": parts, "style": bd["style"]})
        bi += 1
    bounds = rejoined

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
                   "section": "章节标题", "hier_section": "章节标题",
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
        # P0-2a：一级章节标题（# 新章节）是文档分节硬边界，其后的正文不属于
        # 上一题答案（S0060 串节）。把 end_line 收紧到该行；标题行与后续正文
        # 由下一轮 emit_gap 收进 leftovers（文档头），不进入本题 a_text。
        for k in range(i + 1, end_line):
            if kinds[k] == "doc_header" or k in hier_section_lines:
                end_line = k
                break
        b_start, b_end = starts[i], starts[end_line] if end_line < n else total
        # 答案区：剔除页码/目录行/水印（→ leftovers，span 保留在块区间内）
        inc, exc_runs, inc_idx = [], [], []
        for j in range(i + 1, end_line):
            if j in consumed:      # D1 续行已并入题干，不再进答案
                continue
            k = kinds[j]
            why = None
            if k == "page":
                why = "页码"
            elif k == "toc":
                why = "目录行"
            elif k == "section" or k == "hier_section":
                why = "章节标题"
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
        # review2：强证据题干续行从答案区收回题干（a_segs 同步裁剪）。
        if bd["parts"]:
            rq, a_text, a_segs = _reclaim_stem_continuation(bd["parts"][-1][1], a_text, a_segs)
            bd["parts"][-1] = (bd["parts"][-1][0], rq)
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


def _toc_ghost_answer(a: str) -> bool:
    """目录幽灵答案：1-6 个短行、全无句读标点（F2，评审评论2）。

    形态来自目录条目的邻行（“运⾏时常量池\\n直接内存”），不是真答案。
    同题干去重时幽灵让位给实质答案，兜住 TOC 掩码逃逸的条目。
    """
    lines = [x.strip() for x in a.splitlines() if x.strip()]
    if not 1 <= len(lines) <= 6:
        return False
    return all(len(x) <= 24 and not re.search(r"[。！？!?；;：:]", x)
               for x in lines)


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
        if (0 < len(inner) <= 120 and not RE_LIST_ITEM.match(inner)
                and not RE_DECIMAL.match(inner)
                and not inner.rstrip().endswith(("：", ":"))
                and not _is_codeish(inner)
                and _qnorm_len(inner) >= 6
                and ((prev_blank and (RE_KPOINT.match(inner) or _is_qline(inner)))
                     or _is_strong_qline(inner))):
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
        seg_lines = [x.strip() for x in seg.splitlines() if x.strip()]
        first_line = strip_bold(seg_lines[0])   # 题干剥粗体壳
        # D1：答案区内拆出的题同样做换行续接（与主通道同口径）
        if len(seg_lines) > 1 and _is_stem_continuation(first_line, seg_lines[1]):
            first_line = _join_stem(first_line, strip_bold(seg_lines[1]))
        # 一行里“问句 + 紧跟的答案说明”只取问句部分作题干。
        qm = re.match(r'^(.{1,120}?[?？])\s*(.{4,})$', first_line)
        if qm:
            first_line = qm.group(1)
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


# 成对围栏与未闭合围栏（只开不关，OCR/docx 常见）都作为代码段原样保留。
_RE_FENCE_SPLIT = re.compile(r'(```.*?(?:```|$)|~~~.*?(?:~~~|$))', re.S)
_RE_SEPARATOR_LINE = re.compile(r'^[ \t]*(?:-{5,}|\*{3,}|_{3,})[ \t]*$', re.M)
_RE_BLANK_RUN = re.compile(r'\n[ \t]*\n(?:[ \t]*\n)+')


def _clean_answer_text(a: str) -> str:
    """输出展示层清洗（docs/40 §8.5）：删整行分割线、折叠 3+ 连续换行为单个空行。

    围栏代码块（``` / ~~~）内部原样保留，不删分割线、不折叠空行（§8.6 代码
    保留原行）。只改 item['a'] 展示文本；source_span 与原文不丢字（判据⑥）
    保持不动，不影响回溯与覆盖统计。
    """
    if not a:
        return a
    parts = _RE_FENCE_SPLIT.split(a)
    for i in range(0, len(parts), 2):     # 奇数下标是完整围栏段，跳过
        seg = parts[i]
        seg = _RE_SEPARATOR_LINE.sub('', seg)
        seg = _RE_BLANK_RUN.sub('\n\n', seg)
        parts[i] = seg
    return ''.join(parts)


def chunk_v2(md_text: str) -> Dict:
    """切题器 v2：items/probe/coverage/note（判据见模块 docstring）。"""
    res = chunk(md_text, split_long=False)
    items: List[dict] = []
    seq = [0]

    def add(q, a, span, style):
        seq[0] += 1
        items.append({"seq": seq[0], "q": q, "a": _clean_answer_text(a), "span": list(span),
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

    # D2 出口终检：漏过行级判定的代码碎片不产题（docx 无围栏形态）。
    # 内容并入上一题答案、span 顺延保持可回溯；无上题时降为 probe 候选。
    # 不在行分类层拦编号代码行：那会吃掉 qid 递进链，误伤其后的真题。
    cleaned: List[dict] = []
    frag_probe: List[dict] = []
    for it in items:
        if is_code_fragment(it["q"]):
            if cleaned:
                prev = cleaned[-1]
                prev["a"] = (prev["a"] + "\n" + it["q"] + "\n" + it["a"]).strip("\n")
                prev["span"][1] = it["span"][1]
            else:
                frag_probe.append({"span": it["span"], "why": "代码碎片"})
            continue
        cleaned.append(it)
    items = cleaned

    # 同一文档内完全重复的题干（网页标题与正文题号重复）只做保守合并：
    # 空答案 / 极短答案被更完整答案替换；两条都有实质答案时都保留，
    # 避免不同章节下的同题异答被吞掉。
    dedup: Dict[str, dict] = {}
    items_after: List[dict] = []
    for it in items:
        key = re.sub(r'\s+', '', it["q"]).lower()
        if key not in dedup:
            dedup[key] = it
            items_after.append(it)
            continue
        old = dedup[key]
        # F2（评审评论2）：幽灵答案（目录邻行垃圾）让位给实质答案，
        # 必须先于空答案规则，否则“空答案被幽灵替换”会复活幽灵。
        old_ghost = _toc_ghost_answer(old["a"])
        new_ghost = _toc_ghost_answer(it["a"])
        if old_ghost and not new_ghost:
            items_after[items_after.index(old)] = it
            dedup[key] = it
        elif new_ghost and not old_ghost:
            continue
        elif not old["a"].strip() and it["a"].strip():
            items_after[items_after.index(old)] = it
            dedup[key] = it
        elif old["a"].strip() and not it["a"].strip():
            continue
        elif old["a"].strip() and it["a"].strip():
            # 两条都有实质答案：若一条是另一条的前缀/子串（目录摘要 vs 正文答案），
            # 保留更长的一条；否则都保留，交给后续 q_norm 去重/人工复核。
            old_n = re.sub(r'\s+', '', old["a"]).lower()
            new_n = re.sub(r'\s+', '', it["a"]).lower()
            if old_n and new_n and (old_n in new_n or new_n in old_n):
                if len(new_n) > len(old_n):
                    items_after[items_after.index(old)] = it
                    dedup[key] = it
            else:
                items_after.append(it)
        else:
            continue
    items = items_after

    # probe：孤立代码区始终暴露；items 为空时再给 1–3 个候选区块
    probe = []
    for l in res["leftovers"]:
        if l["why"] == "块前代码" and l["span"][1] - l["span"][0] >= 80:
            region_lines = [x.strip() for x in md_text[l["span"][0]:l["span"][1]].splitlines() if x.strip()]
            # 整段都是编号代码行（docx/OCR 无围栏形态）→ 代码碎片，而非一般孤立代码区。
            numbered_code = bool(region_lines) and all(
                re.match(r'^\d{1,3}\s*[.、．)）]', x) and _is_codeish(x) for x in region_lines)
            probe.append({"span": l["span"], "why": "代码碎片" if numbered_code else "孤立代码区"})
            if len(probe) >= 3:
                break
    if not items:
        probe.extend(frag_probe[:max(0, 3 - len(probe))])
    if not items and len(probe) < 3:
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
                why = ("代码碎片" if _is_codeish(s) else
                       "题号行" if _match_question(ln) else
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
    for it in items:                       # 兜底：续块/碎片合并与去重也可能拼回噪声
        it["a"] = _clean_answer_text(it["a"])
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
