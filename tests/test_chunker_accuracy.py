# -*- coding: utf-8 -*-
"""切题准确率回归（2026-11-20 重构，docs/37 已知缺陷 D1/D2/D3）。

D1 题干换行硬切（chunker:line_wrap_split，样本 216294/216309 同形态）：
   PDF/docx 转 md 后题干断成两行，下一行未续回 → 题干截断 + 碎片另成假题。
D2 代码行成题：docx 无 ``` 围栏，编号代码行漏过 _is_codeish → 成为题。
D3 大题说明/多题粘连：「综合大题（五道选两道…）」被当题干；
   答案列表状态把带提问语义的编号子题吞进上一题答案。

阶段 0 以 xfail(strict) 挂起；对应阶段修复后必须转 passed。
"""
import pytest

from mianjing_kb import chunker


# ---------------- D1 题干续行回接 ----------------

D1_NUM_WRAP = """1. 操作系统中进程和线程的

区别是什么？

进程是资源分配单位，线程是调度单位。

2. 什么是死锁？

四个必要条件。
"""

D1_Q_WRAP = """什么是 MySQL 联合索引的

最左前缀原则？

联合索引按最左列匹配查询。
"""

D1_NO_OVERJOIN = """1. 什么是A概念？

什么是B概念？

B 是另一个概念。
"""


def test_d1_numbered_stem_wrapped_line_is_rejoined():
    items = chunker.chunk_v2(D1_NUM_WRAP)["items"]
    qs = [it["q"] for it in items]
    assert len(items) == 2, qs
    assert "进程和线程" in qs[0] and "区别是什么" in qs[0], qs
    assert qs[1] == "什么是死锁？"
    assert "资源分配单位" in items[0]["a"]
    assert "区别是什么" not in items[0]["a"]


def test_d1_question_stem_wrapped_line_is_rejoined():
    items = chunker.chunk_v2(D1_Q_WRAP)["items"]
    qs = [it["q"] for it in items]
    assert len(items) == 1, qs
    assert "联合索引" in qs[0] and "最左前缀原则" in qs[0], qs
    assert "最左列匹配" in items[0]["a"]


def test_d1_complete_questions_are_not_overjoined():
    """守卫：两条完整问句不得被续行规则误并（修复前后都必须成立）。"""
    items = chunker.chunk_v2(D1_NO_OVERJOIN)["items"]
    assert [it["q"] for it in items] == ["什么是A概念？", "什么是B概念？"]


# ---------------- D2 代码碎片出口 ----------------

D2_CODE_LINES = """1. 比较两个字符串的不同

按位比较即可。

2. swap (a, b)

3. scanf ("%d", &n)

4. 通过随机函数生成随机数填充一个二维数组

用 rand 取余。
"""


@pytest.mark.xfail(strict=True, reason="D2 未修复：编号代码行漏过行级判定成为题（阶段2 转绿）")
def test_d2_numbered_code_lines_do_not_become_questions():
    items = chunker.chunk_v2(D2_CODE_LINES)["items"]
    qs = [it["q"] for it in items]
    assert not any("swap" in q for q in qs), qs
    assert not any("scanf" in q for q in qs), qs
    assert any("比较两个字符串" in q for q in qs), qs
    assert any("随机函数" in q for q in qs), qs
    assert not any(chunker.is_code_fragment(q) for q in qs), qs


def test_d2_real_questions_with_code_words_survive():
    """守卫：含代码词的真题目不被出口误杀（修复前后都必须成立）。"""
    md = """1. C++ 中 i++ 和 ++i 的区别?

前置自增先加后用。

2. 写一个函数,可以遍历文件夹下的所有文件和文件夹。

递归遍历即可。
"""
    items = chunker.chunk_v2(md)["items"]
    assert len(items) == 2, [it["q"] for it in items]


# ---------------- D3 大题说明 / 多题粘连 ----------------

D3_EXAM_SECTION = """## 三、综合大题（五道选两道，每题 10 分）

1. 什么是事务的 ACID 特性？

事务具有原子性、一致性、隔离性、持久性。

2. 如何设计一个秒杀系统？

限流、削峰、异步下单。
"""

D3_SUBQUESTIONS_SWALLOWED = """## 1、说说数据库优化的几种方式

答案要点：

1. 索引优化

建立合适索引。

2. 什么是覆盖索引

覆盖索引是查询列都在索引中。

3. 如何避免回表

使用覆盖索引。
"""


@pytest.mark.xfail(strict=True, reason="D3 未修复：大题说明行被当题干（阶段3 转绿）")
def test_d3_exam_section_header_is_not_a_question():
    items = chunker.chunk_v2(D3_EXAM_SECTION)["items"]
    qs = [it["q"] for it in items]
    assert not any("五道选两道" in q or "综合大题" in q for q in qs), qs
    assert any("ACID" in q for q in qs), qs
    assert any("秒杀系统" in q for q in qs), qs


@pytest.mark.xfail(strict=True, reason="D3 未修复：列表状态吞掉带提问语义的子题（阶段3 转绿）")
def test_d3_numbered_subquestions_break_answer_list():
    items = chunker.chunk_v2(D3_SUBQUESTIONS_SWALLOWED)["items"]
    qs = [it["q"] for it in items]
    assert any("覆盖索引" in q and "什么是" in q for q in qs), qs
    assert any("如何避免回表" in q for q in qs), qs
    opt = next(it for it in items if "数据库优化" in it["q"])
    assert "索引优化" in opt["a"]
    assert "什么是覆盖索引" not in opt["a"]


def test_d3_plain_answer_lists_stay_merged():
    """守卫：无提问语义的答案列表仍不得拆题（修复前后都必须成立）。"""
    md = """## 1、说说实现多线程有几种方法

创建线程的常用三种方式：

1. 继承Thread类

2. 实现Runnable接口

3. 实现Callable接口
"""
    items = chunker.chunk_v2(md)["items"]
    assert len(items) == 1, [it["q"] for it in items]
    assert "实现Runnable接口" in items[0]["a"]
