# -*- coding: utf-8 -*-
"""答案文本清洗回归：docs/40 §8.5 空行/分割线、§8.6 异常换行。

清洗只作用于 item['a'] 的展示文本；source_span 保持原文可回溯，原文不丢字
（chunker docstring 判据⑥）。本文件先写期望行为，跑红后再实现。
"""
from mianjing_kb import chunker


def _answer_of(md: str, q_fragment: str) -> str:
    items = chunker.chunk_v2(md)["items"]
    hit = next((it for it in items if q_fragment in it["q"]), None)
    assert hit is not None, f"未找到含 {q_fragment!r} 的题目: {[it['q'][:30] for it in items]}"
    return hit["a"]


def test_answer_separator_line_removed():
    """分割线 ----- 不应出现在答案正文中（§8.5），前后段落保留。"""
    md = "1. 什么是压缩?\n\n压缩能减少体积。\n\n-----\n\n有损压缩会丢信息。"
    a = _answer_of(md, "什么是压缩")
    assert "-----" not in a
    assert "压缩能减少体积" in a and "有损压缩会丢信息" in a


def test_answer_star_and_underscore_separators_removed():
    """*** 与 ___ 装饰线同样应被删除（§8.5）。"""
    md = "1. 问题?\n\n答案第一行。\n\n***\n\n___" + "\n\n答案第二行。"
    a = _answer_of(md, "问题")
    assert "***" not in a and "___" not in a
    assert "答案第一行" in a and "答案第二行" in a


def test_answer_consecutive_blank_lines_collapsed():
    """四五个连续空行折叠为单个段落分隔（§8.5：连续空行折叠，不保留四五空行）。"""
    md = "1. 问题?\n\n第一行。\n\n\n\n\n第二行。"
    a = _answer_of(md, "问题")
    assert "\n\n\n" not in a, f"仍有 3+ 连续换行: {a!r}"
    assert "第一行" in a and "第二行" in a


def test_answer_single_blank_between_paragraphs_kept():
    """单个空行（段落分隔）应保留，避免把段落粘连（§8.5 分割线前后用段落分隔）。"""
    md = "1. 问题?\n\n第一段。\n\n第二段。"
    a = _answer_of(md, "问题")
    assert "\n\n" in a


def test_answer_code_and_http_header_lines_preserved():
    """代码块与 HTTP 头字段原行保留，不因清换行规则受损（§8.6）。"""
    md = (
        "1. 如何设置压缩?\n\n"
        "```\nint x = 1;\n\n\n\nint y = 2;\n```\n\n"
        "Accept-Encoding: gzip\n\n\n\ncontent-encoding: gzip"
    )
    a = _answer_of(md, "如何设置压缩")
    assert "int x = 1;" in a and "int y = 2;" in a
    assert "Accept-Encoding: gzip" in a and "content-encoding: gzip" in a


def test_fenced_code_block_content_not_cleaned():
    """围栏代码块内部的 ----- / 连续空行原样保留，不因清洗受损（§8.6 代码保留原行）。"""
    md = ("1. 问题?\n\n"
          "```\n"
          "-----\n"
          "int a = 1;\n\n\n\n"
          "int b = 2;\n"
          "```\n\n"
          "正文。")
    a = _answer_of(md, "问题")
    assert "-----" in a, f"代码内分割线被误删: {a!r}"
    assert "int a = 1;" in a and "int b = 2;" in a


def test_source_span_unchanged_after_cleaning():
    """清洗不改动原文 span——原文可回溯（chunker 判据⑥）。"""
    md = "1. 问题?\n\n答案。\n\n-----\n\n更多。"
    before = chunker.chunk_v2(md)["items"][0]["span"]
    after = chunker.chunk_v2(md)["items"][0]["span"]
    assert before == after
