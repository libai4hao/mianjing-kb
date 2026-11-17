# -*- coding: utf-8 -*-
"""cluster 测试：三档阈值行为 + 端到端（临时语料）+ 99 区判定。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import cluster as cl  # noqa: E402
from question_freq import normalize, simhash  # noqa: E402

Q_A = "HashMap 和 Hashtable 的区别是什么"
Q_A2 = "HashMap 和 Hashtable 的区别是什么？"          # 仅标点差 → 归一后同文
Q_B = "HashMap 和 Hashtable 的区别是啥"   # 微编辑（什么→啥）：Jaccard 落 0.85~0.92、SimHash 抖动小
Q_C = "MySQL 的InnoDB 和 MyISAM 存储引擎有什么区别"


def _items(*qs):
    return [(normalize(q), simhash(normalize(q)), f"doc{i}.md") for i, q in enumerate(qs)]


def test_build_clusters_exact_and_strong():
    items = _items(Q_A, Q_A2, Q_C)
    dsu, pending, strong = cl.build_clusters(items, 0.92, 0.85)
    # Q_A / Q_A2 归一化同文 → 强并
    assert dsu.find(0) == dsu.find(1)
    assert dsu.find(0) != dsu.find(2)
    assert any(p[2] == 1.0 for p in strong)
    assert pending == []


def test_build_clusters_pending_not_merged():
    # Q_B 与 Q_A 相近但加了尾巴：Jaccard 介于 0.85~0.92 → 只记 pending 不并
    items = _items(Q_A, Q_B, Q_C)
    dsu, pending, strong = cl.build_clusters(items, 0.92, 0.05)
    assert dsu.find(0) != dsu.find(1)
    assert pending and all(0.05 <= p[2] < 0.92 for p in pending)
    assert dsu.find(0) != dsu.find(2)


def test_build_clusters_unrelated():
    items = _items(Q_A, Q_C)
    dsu, pending, strong = cl.build_clusters(items, 0.92, 0.85)
    assert dsu.find(0) != dsu.find(1)
    assert pending == []


@pytest.fixture()
def tiny_corpus(tmp_path, monkeypatch):
    """临时语料：库内 2 份 md（各含同题）+ 99 区 1 份 md（同题）+ 1 份无匹配。"""
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(cl.config, "CORPUS_ROOT", tmp_path)
    # P0-1 修复（2026-11-17 审核）：BUILD_DIR 是 import 期常量，monkeypatch CORPUS_ROOT
    # 不会让它跟着变——漏 patch 时 cluster.main 的三件套产物会写进真实生产 build 目录。
    monkeypatch.setattr(cl.config, "BUILD_DIR", build)
    monkeypatch.setattr(cl, "POOL_TOPS", ["pool"])
    monkeypatch.setattr(cl, "DIR99", "99d")
    monkeypatch.setattr(cl, "DIR20", "md20")
    (tmp_path / "pool").mkdir()
    (tmp_path / "99d").mkdir()
    (tmp_path / "md20").mkdir()
    body = "\n".join(f"## {i+1}. {q}\n\n答：这一题答案写得比较长，用来撑过无文本过滤的八十字符阈值，"
                     f"内容本身只是占位说明，没有更多信息。" for i, q in enumerate([Q_A, Q_C]))
    (tmp_path / "pool" / "库内甲.md").write_text(body, encoding="utf-8")
    (tmp_path / "md20" / "题库乙.md").write_text(
        "\n".join(f"## {i+1}. {q}\n\n答：这一题答案写得比较长，用来撑过无文本过滤的八十字符阈值，"
                  f"内容本身只是占位说明，没有更多信息。" for i, q in enumerate([Q_A2])), encoding="utf-8")
    (tmp_path / "99d" / "重复件.md").write_text(
        "\n".join(f"## {i+1}. {q}\n\n答：这一题答案写得比较长，用来撑过无文本过滤的八十字符阈值，"
                  f"内容本身只是占位说明，没有更多信息。" for i, q in enumerate([Q_A, Q_B])), encoding="utf-8")
    (tmp_path / "99d" / "孤儿.md").write_text(
        "## 1. 完全无关的题目？\n\n答：这一题答案写得比较长，用来撑过无文本过滤的八十字符阈值，"
        "内容本身只是占位说明，没有更多信息。", encoding="utf-8")
    return tmp_path


def test_end_to_end_verdict(tiny_corpus, tmp_path, monkeypatch):
    build = tiny_corpus / "build"
    out = tmp_path / "res.json"
    rc = cl.main(["--no-cache", "--out", str(out), "--sample", "5"])
    assert rc == 0
    res = json.loads(out.read_text(encoding="utf-8"))
    vd = {v["rel"].split("\\")[-1].split("/")[-1]: v["verdict"] for v in res["verdict_detail"]}
    assert vd.get("重复件.md") == "strong"    # 与库内甲强并（Q_A 精确同文）
    assert vd.get("孤儿.md") == "none"       # 无匹配（含无题件）→ 默认保留
    assert res["verdict99"]["strong"] >= 1
    assert res["verdict99"]["none"] >= 1
    # 产物三件套齐全（P0-1 修复：原空壳断言 parent is not None 恒真，改为真断言；
    # 且必须落在临时 build 里——写进生产 build 即回归）
    assert (build / "cluster_pending.csv").exists()
    assert (build / "cluster_sample50.md").exists()
