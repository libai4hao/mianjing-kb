# -*- coding: utf-8 -*-
"""答案边界与 D1 续行回归（S0060 串节 / S0129 续行落入答案）。"""
from mianjing_kb import chunker


def _answer_of(md: str, q_fragment: str) -> str:
    items = chunker.chunk_v2(md)["items"]
    hit = next(it for it in items if q_fragment in it["q"])
    return hit["a"]


def test_doc_header_ends_previous_answer():
    """一级标题（# 新章节）之后的内容不得并入上一题答案（S0060 串节）。

    end_line 遇到 doc_header 类标题应收紧到该行，标题及其后正文进 leftovers，
    而不是作为上一题答案的一部分。
    """
    md = """1、http 的 post 和 get 的区别？
get 传参数在 URL，post 在 body。

# 唯品会的技术岗面试

这是新章节的正文，不属于上一题。
"""
    a = _answer_of(md, "post 和 get 的区别")
    assert "get 传参数在 URL" in a, "答案本体应保留"
    assert "唯品会" not in a, f"新章节标题串入答案: {a!r}"
    assert "新章节的正文" not in a, f"新章节正文串入答案: {a!r}"


def test_verb_ending_stem_reclaims_short_question_continuation():
    """题干以动词结尾 + 紧随极短问句续行 → 回接题干，不落入答案（S0129）。

    "…最后结束" + "的线程?"：末字"束"不是连接词，现有 RE_STEM_DANGLING 不认；
    需要"极短问句续行"兜底分支把续行并回题干。
    """
    md = """你如何确保main()方法所在的线程是Java 程序最后结束
的线程?

我们可以使用Thread 类的join()方法来确保线程结束。
"""
    v2 = chunker.chunk_v2(md)
    items = v2["items"]
    hit = next(it for it in items if "最后结束" in it["q"])
    assert "最后结束的线程" in hit["q"], f"续行未回接题干: {hit['q']!r}"
    assert "的线程?" not in hit["a"] and "的线程？" not in hit["a"], f"续行落入答案: {hit['a']!r}"
    assert "join()" in hit["a"], "答案本体应保留"
