# -*- coding: utf-8 -*-
"""题簇成对评测：加载、判据、P/R/F1、分组、阈值扫描、语料指标与 CLI。"""
from __future__ import annotations

from pathlib import Path

import pytest

from mianjing_kb import eval_cluster as ec
from mianjing_kb.kb import db as kbdb


def _pairs(tmp_path, rows):
    p = tmp_path / "pairs.yaml"
    lines = ["meta:", "  name: t", "  version: 1", "  review_state: draft", "pairs:"]
    for r in rows:
        lines += [f"  - id: {r['id']}", f"    kind: {r['kind']}",
                  f"    same: {str(r['same']).lower()}",
                  f"    a: \"{r['a']}\"", f"    b: \"{r['b']}\""]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def test_load_pairs_ok(tmp_path):
    p = _pairs(tmp_path, [{"id": "X1", "kind": "normalize", "same": True, "a": "a", "b": "b"}])
    meta, rows = ec.load_pairs(p)
    assert meta["review_state"] == "draft" and len(rows) == 1


def test_load_pairs_rejects_bad_same(tmp_path):
    p = tmp_path / "bad.yaml"
    p.write_text("pairs:\n  - id: A\n    kind: normalize\n    same: 'yes'\n    a: x\n    b: y\n",
                 encoding="utf-8")
    with pytest.raises(ValueError, match="same"):
        ec.load_pairs(p)


def test_load_pairs_rejects_duplicate_id(tmp_path):
    p = _pairs(tmp_path, [{"id": "X1", "kind": "normalize", "same": True, "a": "a", "b": "b"},
                          {"id": "X1", "kind": "normalize", "same": False, "a": "c", "b": "d"}])
    with pytest.raises(ValueError, match="重复"):
        ec.load_pairs(p)


def test_pair_features_normalize_equal():
    f = ec.pair_features("什么是 Spring Boot？", "什么是SpringBoot")
    assert f["norm_equal"] and f["hamming"] == 0 and f["jaccard"] == 1.0


def test_score_confusion_math():
    pos = {"a": "什么是 AOP？", "b": "什么是AOP"}
    neg_same_words = {"a": "进程和线程的区别", "b": "协程和线程的区别"}
    far = {"a": "什么是 Redis", "b": "如何设计秒杀系统"}
    rows = [
        {"id": "1", "kind": "normalize", "same": True, **pos,
         "feat": ec.pair_features(**pos)},
        {"id": "2", "kind": "hard_neg", "same": False, **neg_same_words,
         "feat": ec.pair_features(**neg_same_words)},
        {"id": "3", "kind": "paraphrase", "same": True, **far,
         "feat": ec.pair_features(**far)},
        {"id": "4", "kind": "hard_neg", "same": False, **far,
         "feat": ec.pair_features(**far)},
    ]
    s = ec.score(rows, 24, 0.7)
    assert (s["tp"], s["fp"], s["fn"], s["tn"]) == (1, 1, 1, 1)
    assert s["precision"] == 0.5 and s["recall"] == 0.5 and s["f1"] == 0.5
    assert {f["id"] for f in s["fails"]} == {"2", "3"}


def test_by_kind_isolates_paraphrase():
    rows = [
        {"id": "n", "kind": "normalize", "same": True, "a": "x", "b": "x",
         "feat": ec.pair_features("x", "x")},
        {"id": "p", "kind": "paraphrase", "same": True,
         "a": "什么是 AOP？", "b": "说一下你对 AOP 的理解",
         "feat": ec.pair_features("什么是 AOP？", "说一下你对 AOP 的理解")},
    ]
    all_s = ec.score(rows, 24, 0.7)
    para = ec.score(rows, 24, 0.7, "paraphrase")
    assert all_s["tp"] == 1 and all_s["fn"] == 1
    assert para["n"] == 1 and para["recall"] == 0.0


def test_sweep_rows_shape():
    feat = ec.pair_features("什么是 AOP？", "什么是AOP")
    rows = [{"id": "1", "kind": "normalize", "same": True, "a": "a", "b": "b", "feat": feat}]
    out = ec.sweep(rows, dists=(24,), jacs=(0.5, 0.7))
    assert len(out) == 2 and all({"dist", "jac", "precision", "recall", "f1"} <= set(r) for r in out)


def test_db_stats_counts_answer_coverage(tmp_path):
    conn = kbdb.connect(tmp_path / "kb.sqlite")
    for i, a in enumerate(["一段足够长的答案" * 3, "", "短答"]):
        conn.execute(
            "INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
            "qtype, conf, q_norm, q_norm_hash, simhash, extracted_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"d{i}", 1, "什么是 JVM？", a, 0, 10, "八股", "high", "norm", f"h{i}", 0, "now"))
    conn.commit()
    conn.close()
    s = ec.db_stats(tmp_path / "kb.sqlite")
    assert s["questions"] == 3
    assert s["answer_coverage"] == round(1 / 3, 4)
    assert s["empty_answer"] == 1


def test_md_stats_coverage_and_zero_docs(tmp_path):
    root = tmp_path / "md"
    root.mkdir()
    (root / "q.md").write_text(
        "1. 什么是 JVM？\n\nJVM 是 Java 虚拟机，负责执行字节码。\n\n"
        "2. 什么是 GC？\n\n垃圾回收器负责回收不可达对象。\n", encoding="utf-8")
    (root / "empty.md").write_text("# 只有标题\n\n没有任何题目的一句话。\n", encoding="utf-8")
    s = ec.md_stats(root)
    assert s["md_docs"] == 2 and s["docs_zero_items"] >= 1
    assert 0 < s["coverage_micro"] <= 1.0
    assert s["leftover_unrecognized_ratio"] >= 0


def test_cli_writes_report_and_exit_codes(tmp_path):
    p = _pairs(tmp_path, [
        {"id": "N1", "kind": "normalize", "same": True, "a": "什么是Spring", "b": "什么是 Spring？"},
        {"id": "H1", "kind": "hard_neg", "same": False, "a": "进程和线程的区别", "b": "协程和线程的区别"},
    ])
    out = tmp_path / "r.md"
    rc = ec.main(["--pairs", str(p), "--skip-corpus", "--out-md", str(out)])
    assert rc == 0 and "Precision" in out.read_text(encoding="utf-8")
    # H1 在默认阈值会被并（hamming 6 / jac 0.75）→ P=0.5, F1<0.99
    out2 = tmp_path / "r2.md"
    assert ec.main(["--pairs", str(p), "--skip-corpus", "--min-f1", "0.99",
                    "--out-md", str(out2)]) == 1


def test_cli_missing_pairs_returns_2(tmp_path):
    assert ec.main(["--pairs", str(tmp_path / "nope.yaml"), "--skip-corpus"]) == 2


def test_predict_matches_production_predicate():
    """#18：评测谓词与 question_freq.cluster 一致——汉明不再挡对，F6 闸门参与。"""
    # 高 Jaccard 但汉明超 dist：生产会并，评测也必须并
    a, b = "什么是 Spring Boot 的自动配置原理", "什么是 Spring Boot 的自动配置的原理"
    feat = ec.pair_features(a, b)
    assert feat["jaccard"] >= 0.7
    assert ec.predict(feat, dist=0, jac=0.7) is True   # 旧谓词会被 dist=0 挡下
    # 短题拉丁实体冲突：Jaccard 达标也不并
    f2 = ec.pair_features("什么是springmvc", "什么是spring?")
    assert f2["jaccard"] >= 0.7 and f2["short_latin_conflict"]
    assert ec.predict(f2, dist=24, jac=0.7) is False
    # 与生产 cluster 逐对一致
    from mianjing_kb.question_freq import cluster, normalize, simhash
    for x, y, want in ((a, b, True), ("什么是springmvc", "什么是spring?", False)):
        items = [(normalize(x), simhash(normalize(x))), (normalize(y), simhash(normalize(y)))]
        dsu = cluster(items, max_dist=24, min_jac=0.7)
        assert (dsu.find(0) == dsu.find(1)) is want
        assert ec.predict(ec.pair_features(x, y), 24, 0.7) is want


def test_full_eval_missing_inputs_returns_2(tmp_path):
    """#17：全量评测缺库/缺语料 → exit 2，不得静默降级成成对评测。"""
    p = _pairs(tmp_path, [
        {"id": "N1", "kind": "normalize", "same": True, "a": "什么是Spring", "b": "什么是 Spring？"},
    ])
    rc = ec.main(["--pairs", str(p), "--db", str(tmp_path / "no.db"),
                  "--md-root", str(tmp_path / "no-md"), "--out-md", str(tmp_path / "r.md")])
    assert rc == 2


def test_min_f1_requires_reviewed_state(tmp_path, capsys):
    """#19：draft/未知 review_state 不得作验收阈值；reviewed 才允许 --min-f1 判定。"""
    rows = [{"id": "N1", "kind": "normalize", "same": True,
             "a": "什么是Spring", "b": "什么是 Spring？"}]
    p = _pairs(tmp_path, rows)                      # _pairs 写 review_state: draft
    assert ec.main(["--pairs", str(p), "--skip-corpus", "--min-f1", "0.5",
                    "--out-md", str(tmp_path / "r.md")]) == 1
    assert "reviewed" in capsys.readouterr().err
    text = p.read_text(encoding="utf-8").replace("review_state: draft", "review_state: reviewed")
    p.write_text(text, encoding="utf-8")
    assert ec.main(["--pairs", str(p), "--skip-corpus", "--min-f1", "0.5",
                    "--out-md", str(tmp_path / "r2.md")]) == 0
    # 无 --min-f1（0）时 draft 仍可作趋势参考运行
    p.write_text(p.read_text(encoding="utf-8").replace("reviewed", "draft"), encoding="utf-8")
    assert ec.main(["--pairs", str(p), "--skip-corpus",
                    "--out-md", str(tmp_path / "r3.md")]) == 0


def test_cli_refuses_placeholder_output_dir(tmp_path, monkeypatch):
    """未配置 BUILD_DIR 且没传 --out-md：应报错退出，而不是 mkdir 占位路径。"""
    p = _pairs(tmp_path, [
        {"id": "N1", "kind": "normalize", "same": True, "a": "什么是Spring", "b": "什么是 Spring？"},
    ])
    monkeypatch.setattr(ec.config, "BUILD_DIR", Path("<CORPUS_ROOT>") / "21-面经数据库" / "build")
    assert ec.main(["--pairs", str(p), "--skip-corpus"]) == 2
