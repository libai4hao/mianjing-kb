# -*- coding: utf-8 -*-
"""来自 1000 样本审查的真实原文回归（P0-1）。

这些 fixture 不是合成文本，而是从 80 篇原始 Markdown 截取的真实段落；
用于锁定 S0684–S0733、S0078–S0081、S0598/S0599、S0640/S0913、S0921/S0922。
"""
import json
import re
from pathlib import Path

from mianjing_kb import chunker

FIXTURES = Path(__file__).parent / "fixtures"
REAL_DOCS = FIXTURES / "real_docs"


def _items(name: str):
    return chunker.chunk_v2((REAL_DOCS / name).read_text(encoding="utf-8"))["items"]


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def test_50_code_fragments_do_not_become_questions():
    bad = json.loads((FIXTURES / "bad_code_qtexts.json").read_text(encoding="utf-8"))
    items = _items("008_dev_code.md")
    qset = {_norm(it["q"]) for it in items}
    for q in bad:
        assert _norm(q) not in qset, q
    qs = [it["q"] for it in items]
    assert any("比较两个字符串" in q for q in qs)
    assert any("100 亿" in q for q in qs)
    assert any("随机函数" in q for q in qs)


def test_batj_toc_entries_do_not_become_questions():
    items = _items("068_batj_toc_excerpt.md")
    qs = [it["q"] for it in items]
    assert "什么是Mybatis？" in qs
    for prefix in (
        "通常一个Xml 映射文件",
        "在Elasticsearch 中,是怎么根据一个词",
        "(implements)接口?抽象类",
        "Java 中如何格式化一个日期",
    ):
        assert not any(q.startswith(prefix) for q in qs), prefix


def test_tlab_and_acid_boundaries():
    items = _items("029_tlab_acid_excerpt.md")
    tlab = next(it for it in items if "TLAB" in it["q"])
    assert "28、" not in tlab["a"]
    assert "29、" not in tlab["a"]
    assert "对象头具体" not in tlab["a"]
    assert not any("持久性" in it["q"] for it in items)
    assert not any(it["q"].strip() in ("原子性（Atomicity）", "一致性（Consistency）") for it in items)


def test_ali_collection_advantage_list_is_not_questions():
    items = _items("073_collection_list_excerpt.md")
    qs = [it["q"] for it in items]
    assert any("Java集合框架是什么" in q for q in qs)
    assert "复用性和可操作性。" not in qs
    assert not any(q.strip() in ("代码质量会得到提高。", "可以降低代码维护成本。") for q in qs)


def test_ali_thread_heading_and_empty_answer_boundaries():
    items = _items("073_thread_headings_excerpt.md")
    q9 = next(it for it in items if it["q"].startswith("一个线程如果出现了运行时异常"))
    assert "sleep方法和wait方法" not in q9["a"]
    q11 = next(it for it in items if it["q"].startswith("sleep方法和wait方法"))
    assert q11["a"].strip()
    hashtable = next(it for it in items if it["q"].startswith("Hashtable能同时有两条线程"))
    assert hashtable["a"].strip() == ""
    rw = next(it for it in items if it["q"].startswith("ReadWriteLock是什么"))
    assert rw["a"].strip()
