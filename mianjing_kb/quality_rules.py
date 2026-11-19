# -*- coding: utf-8 -*-
"""确定性题目质量预检规则。

只隔离高置信代码/注释片段及明确的区段残片、答案错配、多题混杂；不按长度或
“是否含问号”这类弱信号判题。自然语言中的代码题（如“解释 ++i 和 i++ 的区别？”）
必须保留。

与 2026-11-20 附件包版本的差异（复核修复）：
- 提问/指令语义只在**行首**生效，避免代码注释里的“输出/说明/请求”把代码行
  放回题目队列；
- 声明式代码必须带代码标点（; = , [ ] ( ) { }）才判代码，避免把
  “int 类型占几个字节”这类正常面试题误杀；
- 容忍审核 CSV 为防公式注入加的行首单引号，规则回放不会因导出格式漏检。
"""
from __future__ import annotations

import re

_CJK = re.compile(r"[\u4e00-\u9fff]")
_ASK_HEAD = re.compile(
    r"^(?:请|解释|分析|说明|描述|谈谈|介绍|说说|写出|实现|计算|求|比较|遍历|"
    r"什么是|什么叫|如何|为什么|区别|是否|哪一个|哪种|选择|输出)"
)
_QUESTION_END = re.compile(r"[?？]\s*$")
_CODE_PATTERNS = (
    ("preprocessor", re.compile(r"^\s*#\s*(?:include|define|if|ifdef|ifndef|endif|pragma)\b", re.I)),
    ("comment", re.compile(r"^\s*/\s*\*.*(?:\*/|$)|^\s*//")),
    ("return", re.compile(r"^\s*(?:return\s+|返回\s*)[^。！？?]+\s*;?\s*$", re.I)),
    ("control", re.compile(r"^\s*(?:if|else\s+if|while|for|switch|catch)\s*\(.*$", re.I)),
    ("increment", re.compile(r"^\s*(?:\+\+|--)\s*[\w\u4e00-\u9fff]+\s*;?\s*$")),
    ("increment_list", re.compile(
        r"^\s*(?:\+\+|--)\s*[A-Za-z_]\w*\s*,\s*(?:\+\+|--)\s*[A-Za-z_]\w*\s*\)?\s*;?\s*$"
    )),
    ("condition", re.compile(
        r"^\s*[A-Za-z_]\w*(?:\s*\[[^]]+\])?\s*(?:==|!=|<=|>=|<|>)\s*"
        r"[^！？?]+[;)]?\s*$"
    )),
    ("declaration", re.compile(
        r"^\s*(?:(?:const|static|unsigned|signed|long|short|int|char|float|double|void|size_t|bool|struct|enum)\s+)"
        r"(?:\*\s*)?[A-Za-z_\u4e00-\u9fff]\w*(?:\s*\[[^]]*\])?\s*(?:\([^;]*\))?\s*(?:=\s*[^;]+)?\s*[;,]?\s*$"
    )),
    ("assignment", re.compile(
        r"^\s*[A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff]*(?:\s*\[[^]]+\])?\s*"
        r"(?:=|\+=|-=|\*=|/=)\s*[^。！？?]+[;,]?\s*$"
    )),
    ("call", re.compile(r"^\s*[A-Za-z_]\w*\s*\([^！？?]*$")),
)


def _strip_numbering(text: str) -> str:
    s = re.sub(r"^\s*(?:\d{1,3}\s*[.、．)）]|[（(]\d{1,3}[)）])\s*", "", text)
    return s.strip().strip("`' ")


def code_fragment_reason(text: str | None) -> str | None:
    """返回高置信代码片段原因；自然语言问题返回 None。

    提问词只在行首判定：`// 输出格式说明`、`return 输出结果;` 仍是代码，
    而 `分析以下代码输出什么？`、`解释 return 0 的作用` 仍保留为题目。
    """
    s = _strip_numbering(str(text or "").strip())
    if not s:
        return None
    if _QUESTION_END.search(s) or _ASK_HEAD.search(s):
        return None
    for reason, pattern in _CODE_PATTERNS:
        if not pattern.match(s):
            continue
        # “int 类型占几个字节 / long 占几个字节”是典型面试题，不是声明行。
        # 只含中文自然语言、没有任何代码标点的声明式行不按代码处理。
        if reason == "declaration" and _CJK.search(s) and not re.search(r"[;=,\[\](){}]", s):
            continue
        return reason
    # OCR 后的中文代码返回语句 / C/C++ 常见指针及数组表达式。
    if re.match(r"^\s*(?:结果|[A-Za-z_]\w*)\s*\[[^]]+\]\s*=.+;\s*$", s):
        return "assignment"
    return None


def manual_review_flags(flags: list[str] | tuple[str, ...] | set[str] | None) -> set[str]:
    """需要人工确认、不得只凭 LLM reviewer 放行的质量标记。"""
    return set(flags or ()) & {"残缺", "代码片段", "答案错配", "多题混杂"}


def deterministic_quality_flags(q_text: str | None, a_text: str | None) -> set[str]:
    """识别可由文本结构确定的残缺/错配/多题信号。

    规则有意保守：不以短题干、空答案或长答案单独判无效；只检查明确的
    区段标题、题目包装说明、答案键、多道编号问题/追问和缺失代码上下文。
    """
    q = str(q_text or "").strip()
    a = str(a_text or "").strip()
    flags: set[str] = set()

    # 产品框架/答案提纲被单独切成题目：S028-S031 类。
    if re.match(r"^(?:根本|资源|ROI|需求)\s*[:：∶]", q, re.I):
        flags.add("残缺")
    if ("四个出发点" in q and all(x in q.lower() for x in ("需求", "根本", "资源"))
            and re.search(r"roi|roj", q, re.I)):
        flags.update(("残缺", "多题混杂"))
    # 访谈过程说明/答案式事实陈述不应被当作独立提问：S002/S004/S005 类。
    if re.match(r"^(?:假如|如果).{0,80}(?:他会问你|面试官会问)", q):
        flags.add("残缺")
    if re.match(r"^(?:get|post)\s+(?:传送的数据量|安全性)", q, re.I) and not re.search(r"[?？]|区别|为什么|如何", q):
        flags.add("残缺")
    # “如何解决：1.给出做法”本身是答案措施，不是被问到的问题：S033 类。
    if re.match(r"^如何解决\s*[:：]\s*(?:\d+[.、]|[-*])", q):
        flags.add("残缺")

    # 题目包装语/泛指选择题缺少真正问题或选项上下文：S057/S060 类。
    if re.match(r"^(?:大题|综合题).{0,30}(?:五道|几道|选\s*\d+|任选)", q):
        flags.add("多题混杂")
    if re.match(r"^(?:下面|下列)说法正确的是\s*[（(]?\s*[)）]?$", q) and a:
        flags.add("残缺")
    # 题干声称有程序/代码，但问题块与答案块都没有代码：无法可靠求输出/sizeof。
    if re.search(r"(?:如下|以下)程序|sizeof\s*\(\s*[A-Za-z_]", q, re.I) and not re.search(
            r"[{};]|#include|\b(?:int|char|struct|class|return)\b", q + "\n" + a, re.I):
        flags.add("残缺")
    if re.search(r"一段\s*(?:php|python|java|c\+\+?|c语言)?\s*代码.{0,20}(?:输出结果|运行结果)", q, re.I) and not a:
        flags.add("残缺")

    # 只有篇章标题/目录标题，不能充当答案：S019/S067 类。
    if a and len(a) <= 36 and re.match(
            r"^(?:[（(]?[一二三四五六七八九十0-9]+[)）]?\s*)?"
            r"[\u4e00-\u9fffA-Za-z0-9_、·-]{2,24}(?:篇|部分|笔试题|面经|题库)\s*[:：。．]?$", a):
        flags.add("答案错配")

    if a:
        # 参考答案串通常属于整页/整批选择题答案，不是单题答案：S059 类。
        answer_key_entries = re.findall(r"(?:^|[\s\r\n])\d{1,2}\s*[、.．)]\s*[A-DＡ-Ｄ]", a, re.I)
        if "参考答案" in a and len(answer_key_entries) >= 5:
            flags.add("多题混杂")

        # 答案字段中出现多道带明确问意的编号问题：S003/S066 类。
        numbered_question_heads = re.findall(
            r"(?m)^\s*\d{1,2}\s*[、.．)]\s*[^\r\n]{0,100}"
            r"(?:[?？]|什么|如何|为什么|区别|怎么|作用|原理|要求|写出|了解)", a)
        if len(numbered_question_heads) >= 2:
            flags.add("多题混杂")
        numbered_task_lines = re.findall(
            r"(?m)^\s*\d{1,2}\s*[、.．)]\s*[^\r\n]{0,120}"
            r"(?:题目|要求|代码|如何|了解|区别|怎么|什么|实现|输出)", a)
        if len(numbered_task_lines) >= 3:
            flags.add("多题混杂")

        # 面试对话/复盘里连续“又问/然后问”通常是多个题目的集合：S032 类。
        followups = re.findall(r"(?:然后他问|然后问|又问|接着问|还问|再问)", a)
        if len(followups) >= 3:
            flags.add("多题混杂")

        # 多个独立短行都像问题/任务，视为多题拼接，不作为单题答案：S034 类。
        task_lines = [line.strip() for line in a.splitlines() if line.strip() and len(line.strip()) <= 100
                      and re.search(r"(?:是什么|什么是|如何|怎么|实现一个|写一个|区别|原理|作用|[?？]$)", line)]
        if len(task_lines) >= 4:
            flags.add("多题混杂")

        # 答案明显属于另一个专题文章/群面记录：仅在题干是明确独立技术考点时触发。
        if re.search(r"数字证书|公钥|私钥", q) and re.search(r"群面面经|产品策划/运营", a):
            flags.add("答案错配")

    return flags
