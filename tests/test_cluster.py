# -*- coding: utf-8 -*-
"""cluster 测试：三档阈值行为 + 端到端（临时语料）+ 99 区判定。"""
import json

import pytest
from mianjing_kb import cluster as cl  # noqa: E402
from mianjing_kb.question_freq import normalize, simhash  # noqa: E402

Q_A = "HashMap 和 Hashtable 的区别是什么"
Q_A2 = "HashMap 和 Hashtable 的区别是什么？"          # 仅标点差 → 归一后同文
Q_B = "HashMap 和 Hashtable 的区别是啥"   # 微编辑（什么→啥）：Jaccard 落 0.85~0.92、SimHash 抖动小
Q_C = "MySQL 的InnoDB 和 MyISAM 存储引擎有什么区别"


def _items(*qs):
    return [(normalize(q), simhash(normalize(q)), f"doc{i}.md") for i, q in enumerate(qs)]


def test_build_clusters_exact_and_strong():
    items = _items(Q_A, Q_A2, Q_C)
    dsu, pending, strong, _conf = cl.build_clusters(items, 0.92, 0.85)
    # Q_A / Q_A2 归一化同文 → 强并
    assert dsu.find(0) == dsu.find(1)
    assert dsu.find(0) != dsu.find(2)
    assert any(p[2] == 1.0 for p in strong)
    assert pending == []


def test_build_clusters_pending_not_merged():
    # Q_B 与 Q_A 相近但加了尾巴：Jaccard 介于 0.85~0.92 → 只记 pending 不并
    items = _items(Q_A, Q_B, Q_C)
    dsu, pending, strong, _conf = cl.build_clusters(items, 0.92, 0.05)
    assert dsu.find(0) != dsu.find(1)
    assert pending and all(0.05 <= p[2] < 0.92 for p in pending)
    assert dsu.find(0) != dsu.find(2)


def test_build_clusters_unrelated():
    items = _items(Q_A, Q_C)
    dsu, pending, strong, _conf = cl.build_clusters(items, 0.92, 0.85)
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
                  f"内容本身只是占位说明，没有更多信息。" for i, q in enumerate([Q_A, Q_A2])), encoding="utf-8")
    (tmp_path / "99d" / "孤儿.md").write_text(
        "## 1. 完全无关的题目？\n\n答：这一题答案写得比较长，用来撑过无文本过滤的八十字符阈值，"
        "内容本身只是占位说明，没有更多信息。", encoding="utf-8")
    monkeypatch.setattr(cl, "load_pool_status", lambda: {
        "pool/库内甲.md": {"status": "processed", "gate": None},
        "md20/题库乙.md": {"status": "processed", "gate": None},
    })
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


def _corpus(tmp_path, monkeypatch, pool_body: str, q99_body: str, status="processed"):
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(cl.config, "CORPUS_ROOT", tmp_path)
    monkeypatch.setattr(cl.config, "BUILD_DIR", build)
    monkeypatch.setattr(cl, "POOL_TOPS", ["pool"])
    monkeypatch.setattr(cl, "DIR99", "99d")
    monkeypatch.setattr(cl, "DIR20", "md20")
    for d in ("pool", "99d", "md20"):
        (tmp_path / d).mkdir()
    (tmp_path / "pool" / "池.md").write_text(pool_body, encoding="utf-8")
    (tmp_path / "99d" / "件.md").write_text(q99_body, encoding="utf-8")
    monkeypatch.setattr(cl, "load_pool_status", lambda: {
        "pool/池.md": {"status": status, "gate": None}})
    return build, tmp_path


def test_single_match_in_large_file_is_manual_not_strong(tmp_path, monkeypatch):
    """200 题里只命中 1 题：占比 0.5% < 90%，整件不得标 strong（review HIGH）。"""
    _corpus(tmp_path, monkeypatch,
                       f"## 1. {Q_A}\n\n答：" + "池答案" * 30,
                       "\n".join(f"## {i+1}. 完全无关题{i}？\n\n答：" + "答案" * 30
                                 for i in range(10)) + f"\n## 11. {Q_A}\n\n答：" + "答案" * 30)
    out = tmp_path / "r.json"
    assert cl.main(["--no-cache", "--out", str(out)]) == 0
    d = {v["rel"]: v for v in json.loads(out.read_text(encoding="utf-8"))["verdict_detail"]}
    rec = d["99d\\件.md"]
    assert rec["verdict"] == "manual"
    assert rec["match_ratio"] < cl.MIN_MATCH_RATIO
    assert any(r.startswith("match_ratio") for r in rec["reasons"])


def test_pool_answer_missing_is_conflict_not_strong(tmp_path, monkeypatch):
    """99 有答案、池版本没识别出答案 → 不能判 strong（删除会丢答案）。"""
    pool_body = f"## 1. {Q_A}\n\n答：无\n\n" + "补充说明文字" * 20
    q99_body = f"## 1. {Q_A}\n\n答：" + "这是 99 区完整答案" * 20
    _corpus(tmp_path, monkeypatch, pool_body, q99_body)
    out = tmp_path / "r.json"
    assert cl.main(["--no-cache", "--out", str(out)]) == 0
    rec = {v["rel"]: v for v in json.loads(out.read_text(encoding="utf-8"))["verdict_detail"]}["99d\\件.md"]
    assert rec["verdict"] == "manual"
    assert rec["answer_conflicts"] and rec["answer_conflicts"][0]["why"] == "pool_answer_missing_or_differs"


def test_invalid_pool_status_blocks_strong(tmp_path, monkeypatch):
    """池样本在账本里是 rejected：即便题面同文也不能当删除见证。"""
    body = f"## 1. {Q_A}\n\n答：" + "答案" * 30
    _corpus(tmp_path, monkeypatch, body, body, status="rejected")
    out = tmp_path / "r.json"
    assert cl.main(["--no-cache", "--out", str(out)]) == 0
    rec = {v["rel"]: v for v in json.loads(out.read_text(encoding="utf-8"))["verdict_detail"]}["99d\\件.md"]
    assert rec["verdict"] == "manual"
    assert any(r.startswith("pool_status_invalid") for r in rec["reasons"])


def test_default_limit_does_not_overwrite_full_result(tiny_corpus, tmp_path, monkeypatch):
    """无 --out + --limit 时，调试结果不得覆盖 cluster_result.json（review）。"""
    build = tiny_corpus / "build"
    (build / "cluster_result.json").write_text('{"old":1}', encoding="utf-8")
    assert cl.main(["--no-cache", "--limit", "1"]) == 0
    assert json.loads((build / "cluster_result.json").read_text(encoding="utf-8")) == {"old": 1}
    dbg = json.loads((build / "cluster_result_limit1.json").read_text(encoding="utf-8"))
    assert dbg["complete"] is False and dbg["limit"] == 1
    assert (build / "cluster_pending_limit1.csv").exists()

def test_no_cache_does_not_wipe_cache_file(tmp_path):
    """--no-cache 是只读旁路，不得把已有缓存清空（review 自检发现）。"""
    p = tmp_path / "cache.json"
    p.write_text('{"keep": 1}', encoding="utf-8")
    c = cl.TextCache(p, enabled=False)
    c.put("x", {"questions": []})
    c.save()
    assert json.loads(p.read_text(encoding="utf-8")) == {"keep": 1}


def test_limit_without_existing_result_never_uses_full_name(tiny_corpus, tmp_path, monkeypatch):
    """全量结果不存在时，--limit 也不得占用 cluster_result.json。"""
    build = tiny_corpus / "build"
    assert not (build / "cluster_result.json").exists()
    assert cl.main(["--no-cache", "--limit", "1"]) == 0
    assert not (build / "cluster_result.json").exists()
    assert (build / "cluster_result_limit1.json").exists()

def test_lsh_recall_estimate_finds_exact_match():
    items = _items(Q_A, Q_C, Q_A2)
    r = cl.lsh_recall_estimate(items, 2, 0.92, sample=1)
    assert r["sampled"] == 1 and r["truth_pairs"] == 1 and r["recall"] == 1.0


def test_repeated_blocks_cannot_pad_match_ratio(tmp_path, monkeypatch):
    """#8：19 份重复命中题 + 1 份独特未匹配题 → 去重占比 0.5，不得判 strong。"""
    dup = f"1. {Q_A}\n答：" + "答案" * 30
    pool_body = dup
    q99_body = "\n".join([dup] * 19 + ["20. Redis 持久化机制是什么？\n答：" + "另一答案" * 30])
    _corpus(tmp_path, monkeypatch, pool_body, q99_body)
    out = tmp_path / "r.json"
    assert cl.main(["--no-cache", "--out", str(out)]) == 0
    rec = {v["rel"]: v for v in json.loads(out.read_text(encoding="utf-8"))["verdict_detail"]}["99d\\件.md"]
    assert rec["match_ratio"] >= 0.9            # 出现次数口径被撑高
    assert rec["distinct_match_ratio"] == 0.5   # 去重口径只有一半
    assert rec["verdict"] != "strong"
    assert any("unmatched_distinct" in r for r in rec["reasons"])


def test_short_meaningful_answer_conflict_is_manual(tmp_path, monkeypatch):
    """#12：99 短答案"线程不安全"、池无答案 → 冲突降 manual，不再被短答案闸放行。"""
    pool_body = f"1. {Q_A}？\n" + "补充说明文字" * 20      # 池版本无答案标记
    q99_body = f"1. {Q_A}？\n答：线程不安全\n" + "补充说明文字" * 20
    _corpus(tmp_path, monkeypatch, pool_body, q99_body)
    out = tmp_path / "r.json"
    assert cl.main(["--no-cache", "--out", str(out)]) == 0
    rec = {v["rel"]: v for v in json.loads(out.read_text(encoding="utf-8"))["verdict_detail"]}["99d\\件.md"]
    assert rec["verdict"] == "manual"
    assert rec["answer_conflicts"]


def test_cache_key_includes_content_hash(tmp_path):
    """#10：同尺寸同 mtime 换件 → 缓存键不同，不复用旧抽取。"""
    import os
    p = tmp_path / "a.md"
    p.write_text("甲内容", encoding="utf-8")
    st = p.stat()
    c = cl.TextCache(tmp_path / "cache.json")
    cached1, key1 = c.get(p)
    assert cached1 is None
    c.put(key1, {"questions": [["旧题"]], "text_chars": 3})
    # 换内容但恢复 size/mtime（同长度不同字节）
    p.write_text("乙内容", encoding="utf-8")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    cached2, key2 = c.get(p)
    assert key1 != key2 and cached2 is None


def test_witness_content_mismatch_blocks_strong(tmp_path, monkeypatch):
    """#11：台账状态合法但池文件字节与登记 sha256 不符 → 见证 invalid。"""
    body = f"## 1. {Q_A}\n\n答：" + "答案" * 30
    build, root = _corpus(tmp_path, monkeypatch, body, body)
    import hashlib
    real_sha = hashlib.sha256((root / "pool" / "池.md").read_bytes()).hexdigest()
    monkeypatch.setattr(cl, "load_pool_status", lambda: {
        "pool/池.md": {"status": "processed", "gate": None, "sha256": "0" * 64}})
    out = tmp_path / "r.json"
    assert cl.main(["--no-cache", "--out", str(out)]) == 0
    rec = {v["rel"]: v for v in json.loads(out.read_text(encoding="utf-8"))["verdict_detail"]}["99d\\件.md"]
    assert rec["verdict"] == "manual"
    assert any(r.startswith("pool_status_invalid") for r in rec["reasons"])
    # 对照：sha256 一致时仍可 strong
    monkeypatch.setattr(cl, "load_pool_status", lambda: {
        "pool/池.md": {"status": "processed", "gate": None, "sha256": real_sha}})
    out2 = tmp_path / "r2.json"
    assert cl.main(["--no-cache", "--out", str(out2)]) == 0
    rec2 = {v["rel"]: v for v in json.loads(out2.read_text(encoding="utf-8"))["verdict_detail"]}["99d\\件.md"]
    assert rec2["verdict"] == "strong"
