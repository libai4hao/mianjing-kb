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
    r'(?:如下|以下|下列|包括|包含|分为|分成|分别是|基本要素|几个方面|哪几种|哪些|几种|四种|三种)')
RE_NESTED_NUM = re.compile(
    r'^\s*(?:#{1,6}\s+)?((?:\d{1,3}\s*[.、．]\s*){2,})([^\d].+)$')
RE_HIER_NUM = re.compile(
    r'^\s*(?:#{1,6}\s+)?(\d{1,3}(?:\.\d{1,3}){2,3})\s+(.+)$')


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


def _toc_mask(lines: List[str]) -> set[int]:
    """识别文首目录区，返回需要标为 toc 的行号集合。

    目录经常跨行、跨页，不能只靠“点线+页码”的单行正则。这里以“目录”标题为
    起点，以第一个非目录的 H1/H2 标题（或连续页码后的首个正文行）为终点。
    """
    n = len(lines)
    start = None
    for i in range(min(n, 240)):
        if RE_TOC_HEADER.match(lines[i].strip()):
            start = i
            break
    if start is None:
        return set()
    leaders = pages = 0
    saw_entry = False
    end = n
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
        if RE_MD_H12.match(s) and not RE_TOC.search(s) and leaders >= 2:
            end = j
            break
        # 有目录条目、但后面没有 H1/H2 时，遇到第一个真正题目行就结束，
        # 避免把正文全部当作目录。
        if (saw_entry and not RE_TOC.search(s)
                and (_match_question(s) or _is_qline(s))
                and not any(RE_MD_H12.match(x.strip()) for x in lines[j:j + 5000])):
            end = j
            break
        # 无 Markdown 标题的 PDF 目录：连续页码后，首个不缩进、非目录样式行视为正文。
        # 但若后面 5000 行内还有 H1/H2，优先等真正的正文标题，避免目录页脚提前截断。
        future_h12 = any(RE_MD_H12.match(x.strip()) for x in lines[j:j + 5000])
        if (pages >= 3 and not future_h12 and not s.startswith((" ", "\t"))
                and not RE_TOC.search(s) and len(CJK.findall(s)) >= 4):
            end = j
            break
    if not saw_entry:
        # 只有“目录”标题、后面没有点线/页码条目：不能把全文当目录。
        return {start}
    return set(range(start, end))


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


def _is_qline(s: str) -> bool:
    """v2 问句行判定：以？结尾，或问词出现在行首 8 字内（防"做详细介绍"类行中误命中）。
    整行粗体先剥壳（`**说说 Spring AOP**` → `说说 Spring AOP`）。"""
    s = strip_bold(s.strip())
    if RE_QEND.search(s):
        return True
    # 选择题常以空括号收尾：`瀑布模型开发流程（）`
    if re.search(r'[（(]\s*[)）]\s*$', s):
        return True
    # 结论/过渡/说明句里的问词不是题干。
    if re.match(r'^(?:这就是|所以|因此|也就是说|综上|总之|那么|可见|于是|'
                r'至于|先解释|首先|其次|再次|然后|最后|先说|再说|接着|需要)', s):
        return False
    if re.match(r'^(?:的|了|是|在|和|与|及|并|而|但|就|也|都|其|此|该)\s', s):
        return False
    if re.match(r'^[A-Za-z][A-Za-z0-9_\s]*是', s):
        return False
    return RE_INTERROGATIVE.search(s[:8]) is not None


def _trim_question_text(s: str) -> str:
    """一行里“问句 + 紧跟的答案说明”只保留问句部分。"""
    m = re.match(r'^(.{1,120}?[?？])\s*(.{4,})$', s.strip())
    return m.group(1) if m else s.strip()


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
    if re.match(r'^[A-Za-z][A-Za-z0-9_\s]*是', s):
        return False
    if _is_qline(s) or RE_KPOINT.match(s):
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
    toc_lines = _toc_mask(lines)
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
    last_qid: Optional[int] = None
    prev_blank = True
    last_problem_mark = False   # v2 增强：问题N 纯标记行武装紧随的独立问句行
    list_active = False         # 普通编号列表：1. 原子性 / 4. 持久性 不是题
    list_pending = False        # 上一行是“如下/以下/四个要素”等列表引导
    for i, ln in enumerate(lines):
        if kinds[i] != "text" or ln.strip() in watermark:
            # 页码/目录/页眉/水印只是"扣除区"，不能消费块级空行；否则 `12\n3. 下一题`
            # 会把空行前置条件清掉，导致下一题被并进上一题。
            if not ln.strip():
                prev_blank = True
            elif kinds[i] == "doc_header":
                prev_blank = True     # 标题/文档头也是块边界，下一行可新开题
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
            if ((prev_blank or last_problem_mark) and 7 < len(inner) <= 160
                    and not RE_LIST_ITEM.match(inner) and not RE_DECIMAL.match(inner)
                    and not inner.rstrip().endswith(("：", ":"))
                    and not _is_codeish(inner)
                    and _is_qline(inner)):
                bounds.append({"line": i, "parts": [(None, _trim_question_text(inner))], "style": "问句"})
                list_pending = False
                list_active = False
            last_problem_mark = False
            prev_blank = False
            continue
        qid, qtext, style = q
        qtext = re.sub(r'^\d{1,3}\s*[，,]\s*', '', qtext)
        qtext = qtext.replace("**", "").strip().lstrip("、，,。；;：:")
        if style == "标题":
            # 章节标题（“MyBatis 面试题”“Java 并发编程（一）”）不是题；只有带
            # 提问语义的标题（“什么是 JVM？”）才作为标题题块。
            if (_is_codeish(qtext)
                    or re.match(r'^(?:有一点|注意|总结|如下|以下|主要|首先|其次|最后)', qtext)
                    or not _is_question_heading(qtext)):
                last_qid = None if (s.startswith("# ") or _looks_section_line(qtext)
                                    or "面试题" in qtext) else last_qid
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
        sequential_heading = (qid == 1 if last_qid is None else qid == last_qid + 1)
        answer_label = bool(re.match(
            r'^(?:知识点|总结|汇总|详解|概述|简介|本地方法栈|程序计数器|方法区|虚拟机栈|堆|栈)$',
            qtext.strip()))
        if (md_num_heading and not _is_question_heading(qtext)
                and not (sequential_heading and not answer_label)):
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
        if (list_active and RE_LIST_ITEM.match(s) and not md_num_heading
                and not qtext.rstrip().endswith(("?", "？"))):
            list_active = True
            list_pending = False
            prev_blank = False
            last_problem_mark = False
            continue
        if (not md_num_heading and (list_active or list_pending or implicit_list)
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
                and not _is_codeish(inner)
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
        if not old["a"].strip() and it["a"].strip():
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
            probe.append({"span": l["span"], "why": "孤立代码区"})
            if len(probe) >= 3:
                break
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
