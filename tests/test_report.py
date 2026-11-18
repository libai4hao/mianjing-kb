# -*- coding: utf-8 -*-
"""report（P1-4 频次报告）回归测试：build_clusters 纯函数路径，不碰真实库。"""
from mianjing_kb.question_freq import simhash
from mianjing_kb.report import build_clusters


def _q(norm, doc="d1", qtype="八股"):
    return {"doc_hash": doc, "seq": 1, "q_text": norm, "q_norm": norm,
            "q_norm_hash": norm[:16], "simhash": simhash(norm), "simhash_u": simhash(norm),
            "qtype": qtype, "file_name": f"{doc}.md", "topic": None}


def test_exact_dup_and_distinct():
    qs = [_q("什么是redis持久化", "a"), _q("什么是redis持久化", "b"),
          _q("完全不同的另外一道题目呢", "c")]
    clusters = build_clusters(qs, max_dist=24, min_jac=0.7)
    multi = [c for c in clusters if c["n_docs"] >= 2]
    assert len(multi) == 1
    assert multi[0]["n_docs"] == 2 and multi[0]["n_variants"] == 1
    assert len(clusters) == 2  # 第三题独立成簇


def test_near_dup_rewrite_merges():
    # 注意：LSH 分带召回是概率性的——用验证过撞桶的用例（band 命中 + ham9 + jac0.8）；
    # hamming 小但四条带都不同的改写对会被漏掉，属已知取舍（P1-4 报告口径已注明）
    qs = [_q("mysql索引为什么用b+树", "a"),
          _q("mysql索引为什么采用b+树", "b")]
    clusters = build_clusters(qs, max_dist=24, min_jac=0.7)
    assert len([c for c in clusters if c["n_docs"] >= 2]) == 1
