# -*- coding: utf-8 -*-
"""qsalvage：题答同行拆分 / 目录行判定 / 目录区连续串识别 / 空白答案归一。"""
import sqlite3

from mianjing_kb.qsalvage import (
    blank_whitespace_answers,
    split_inline_answer,
    toc_row_reason,
    toc_runs,
)


class TestSplitInlineAnswer:
    def test_docx_single_line_question_and_answer(self):
        q = "什么是 MyBatis？答：MyBatis 是一个可以自定义 SQL、存储过程和高级映射的持久层框架。"
        got = split_inline_answer(q)
        assert got is not None
        head, tail = got
        assert head == "什么是 MyBatis？"
        assert tail.startswith("MyBatis 是一个")

    def test_answer_key_variant(self):
        got = split_inline_answer("HashMap 的扩容机制？答案: 负载因子 0.75，容量翻倍。")
        assert got is not None
        assert got[0] == "HashMap 的扩容机制？"

    def test_marker_at_head_is_not_split(self):
        # 「答：…」打头说明这一行本来只有答案，题干在上面，不能据此造题
        assert split_inline_answer("答：配置文件设置 usegeneratedkeys 为 true 31、在 mapper 中如何传递多个参数？") is None

    def test_plain_question_untouched(self):
        assert split_inline_answer("什么是CAS，它有什么特性2") is None
        assert split_inline_answer("synchronized 和 Lock 的区别？") is None

    def test_requires_ask_shape_before_marker(self):
        assert split_inline_answer("事务回滚机制答：要么全做要么全不做") is None


class TestTocRowReason:
    def test_dot_leader_with_page(self):
        assert toc_row_reason("服务续约 ...... 54") == "dot_leader_page"
        assert toc_row_reason("Spring-Core ........…………... ..……………………… ..4") == "dot_leader_page"
        assert toc_row_reason("JAVA 基础........... . ...................................................") is None

    def test_code_and_sentence_not_toc(self):
        # 省略号出现在代码题里，没有行尾页码 → 不是目录
        assert toc_row_reason("sizeof(p1...p4) =?") is None
        assert toc_row_reason("其他 balabala...") is None

    def test_long_line_not_toc(self):
        q = "什么是字节码？采用字节码的大好处是什么" + "…" * 40 + " 12"
        assert toc_row_reason(q) is None


class TestBlankWhitespaceAnswers:
    def test_whitespace_only_answer_becomes_null(self):
        # SQLite 的 trim() 默认不去换行，两种空答案口径曾差 20 条（docs/44）
        conn = sqlite3.connect(":memory:")
        conn.execute("create table questions (qid integer primary key, a_text text)")
        conn.executemany("insert into questions (qid,a_text) values (?,?)",
                         [(1, "\n\n"), (2, "  \t\n "), (3, "有答案"), (4, None), (5, "")])
        assert blank_whitespace_answers(conn) == 3
        left = dict(conn.execute("select qid, a_text from questions"))
        assert left[1] == "" and left[2] == "" and left[5] == "" and left[3] == "有答案"


class TestTocRuns:
    def _rows(self, qs):
        return [(i + 1, q, "") for i, q in enumerate(qs)]

    def test_title_run_detected(self):
        rows = self._rows(["基础语法", "数据类型", "运算符", "关键字", "变量与方法",
                           "面向对象", "集合", "异常", "IO 流"])
        runs = toc_runs(rows, min_len=8)
        assert len(runs) == 1 and len(runs[0]) == 9

    def test_question_run_not_flagged(self):
        rows = self._rows(["什么是 Java？", "如何创建线程？", "为什么用 B+ 树？", "JVM 内存怎么划分？",
                           "GC 有哪几种？", "索引失效的原因有哪些？", "什么是 CAS？", "简述 AQS 原理"])
        assert toc_runs(rows, min_len=8) == []

    def test_short_run_below_min_len(self):
        rows = self._rows(["基础语法", "数据类型", "运算符"])
        assert toc_runs(rows, min_len=8) == []

    def test_run_breaks_on_answered_block(self):
        rows = [(1, "基础语法", ""), (2, "数据类型", ""), (3, "运算符", ""), (4, "关键字", ""),
                (5, "变量与方法", "局部变量在栈上分配"), (6, "面向对象", ""), (7, "集合", ""),
                (8, "异常", ""), (9, "IO 流", "")]
        assert toc_runs(rows, min_len=8) == []
