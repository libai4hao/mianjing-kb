# -*- coding: utf-8 -*-
"""题干显示层清洗：剥掉转换产物里的 Markdown 强调符与题号（F4）。

来源：pymupdf4llm 会把 PDF 的加粗转成 `**…**`，切题器原样保留，
落在 `questions.q_text` 里就是 `**1** 、什么是 **Redis** ?`——展示脏，整句 FTS 也难命中。

两条硬约束：
  1. **只洗题干（q_text）**，`a_text` 一律不动：答案里常含代码块，`int** p`、`__init__`
     这类写法被“清理”一次就没法还原了。
  2. 题干自身可能是代码题（多行、围栏、反引号、花括号、分号）或含指针写法
     （`int**` / `char**`），这类整段原样返回，只做首尾空白修剪。

口径说明：清洗只改显示文本，不参与 `q_norm` / `q_norm_hash` 的计算——
数字题号在 `normalize` 里本来就会被保留（`**1** 、` → `1`），若在这里连题号一起
喂给 `normalize`，全库 `q_key` 会漂移，标签与向量都要迁移。身份稳定性优先。
"""
from __future__ import annotations

import re

# 出现这些迹象就当“代码题干”，一个字符都不改
CODE_HINTS = ("```", "`", "\n", "{", "}", ";", "\t")
POINTER_SYNTAX = re.compile(r"[A-Za-z0-9_]\*\*")     # int** / char** 等指针声明
BOLD_PAIR = re.compile(r"\*\*(.+?)\*\*")
LEAD_NUMBER = re.compile(
    r"^(?:第\s*)?(?:[（(]\d{1,3}[）)]\s*|\d{1,3}[）)]?\s*题?\s*[、.．,，:：]\s*)")
SPACES = re.compile(r"[ \u3000\u00a0]+")
SPACE_BEFORE_PUNCT = re.compile(r"\s+(?=[?？!！,，。.、;；:：])")


def _looks_like_code(text: str) -> bool:
    if any(h in text for h in CODE_HINTS):
        return True
    # 指针写法（int** / char**）只在 `**` 个数为奇数时认：偶数个说明是成对的
    # 加粗标记（`**1** 、什么是 **Redis** ?`），否则 "1**" 会被误判成指针。
    return text.count("**") % 2 == 1 and bool(POINTER_SYNTAX.search(text))


def _strip_leading_number(text: str) -> str:
    """只剥“行首编号 + 分隔符”；`1.5 版本` 这种小数/版本号保持原样。"""
    m = LEAD_NUMBER.match(text)
    if not m:
        return text
    rest = text[m.end():].lstrip()
    if not rest or rest[0].isdigit():
        return text
    return rest


def _unwrap_bold(text: str) -> str:
    # 反复解，处理 `**1** 、**Redis**` 这种一行多个加粗段；奇数个 ** 时不匹配，保持原样
    for _ in range(3):
        new = BOLD_PAIR.sub(r"\1", text)
        if new == text:
            break
        text = new
    return text


def clean_question_text(text: str) -> str:
    """清洗题干显示文本；代码/指针类题干原样返回（仅 strip）。

    只有真的解掉了 `**` 或剥掉了题号才做空格整理：否则 TOC 点线、
    乱码行这类文本会被顺带改动，清洗范围就超出 F4 了。
    """
    if not text:
        return text
    text = text.strip()          # chunker 的 q 常带行尾换行，先去掉再判“是否多行代码”
    if _looks_like_code(text):
        return text
    out = _strip_leading_number(_unwrap_bold(text))
    if out == text:
        return text
    out = SPACES.sub(" ", out)
    out = SPACE_BEFORE_PUNCT.sub("", out)
    return out.strip()
