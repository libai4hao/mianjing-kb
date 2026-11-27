# -*- coding: utf-8 -*-
"""切题回归基线（docs/47 P0-3）：计量与容差判定。"""
from mianjing_kb import chunk_regress as cr


MD = """# 面经

## 1. 什么是 JVM 的类加载？
类加载分为加载、验证、准备、解析、初始化五步。

## 2. Redis 为什么快？
纯内存 + 单线程 + IO 多路复用。

## 知识点汇总
本节只是答案小节，不是题。
"""


def test_measure_reports_four_metrics():
    m = cr.measure(MD)
    assert set(m) == {"questions", "empty_answers", "weak_questions", "probe"}
    assert m["questions"] >= 2
    assert m["empty_answers"] >= 0
    assert m["weak_questions"] >= 0


def test_compare_passes_on_identical():
    base = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 3},
            "per_doc": {"h1": {"questions": 100}}}
    cur = {"total": {"questions": 101, "empty_answers": 10, "weak_questions": 3},
           "per_doc": {"h1": {"questions": 101}}}
    assert cr.compare(cur, base)["verdict"] == "PASS"


def test_compare_fails_on_question_jump():
    base = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 3},
            "per_doc": {"h1": {"questions": 100}}}
    cur = {"total": {"questions": 140, "empty_answers": 10, "weak_questions": 3},
           "per_doc": {"h1": {"questions": 140}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("questions" in f for f in v["fails"])


def test_compare_fails_when_weak_questions_grow():
    base = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 3},
            "per_doc": {"h1": {"questions": 100}}}
    cur = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 4},
           "per_doc": {"h1": {"questions": 100}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("weak_questions" in f for f in v["fails"])


def test_compare_fails_on_missing_mirror():
    base = {"total": {"questions": 10, "empty_answers": 0, "weak_questions": 0},
            "per_doc": {"h1": {"questions": 10}}}
    cur = {"total": {"questions": 0, "empty_answers": 0, "weak_questions": 0},
           "per_doc": {"h1": {"missing": True}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("镜像缺失" in f for f in v["fails"])


def test_incomplete_baseline_cannot_pass():
    """#5：基线侧 missing 不再被跳过；清单文档在基线缺失同样 FAIL。"""
    base = {"missing": 2, "total": {"questions": 0, "empty_answers": 0, "weak_questions": 0},
            "per_doc": {"h1": {"missing": True}, "h2": {"missing": True}}}
    cur = {"missing": 2, "total": {"questions": 0, "empty_answers": 0, "weak_questions": 0},
           "per_doc": {"h1": {"missing": True}, "h2": {"missing": True}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("基线不完整" in f for f in v["fails"])
    # 当前清单有 h3 而基线没有 → 基线与清单不一致
    cur2 = {"missing": 0, "total": {"questions": 5, "empty_answers": 0, "weak_questions": 0},
            "per_doc": {"h3": {"questions": 5}}}
    base2 = {"missing": 0, "total": {"questions": 5, "empty_answers": 0, "weak_questions": 0},
             "per_doc": {"h1": {"questions": 5}}}
    v2 = cr.compare(cur2, base2)
    assert v2["verdict"] == "FAIL" and any("基线缺少该文档" in f for f in v2["fails"])


def test_content_hash_change_fails_even_within_tolerance():
    """#6：计数在容差内但 md 内容已变 → FAIL（须显式重建基线）。"""
    base = {"missing": 0, "total": {"questions": 10, "empty_answers": 0, "weak_questions": 0},
            "per_doc": {"h1": {"questions": 10, "md_hash": "a" * 64}}}
    cur = {"missing": 0, "total": {"questions": 10, "empty_answers": 0, "weak_questions": 0},
           "per_doc": {"h1": {"questions": 10, "md_hash": "b" * 64}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("内容已变化" in f for f in v["fails"])
    # 旧基线无 md_hash：保持旧行为（只比计数）
    base_old = {"missing": 0, "total": {"questions": 10, "empty_answers": 0, "weak_questions": 0},
                "per_doc": {"h1": {"questions": 10}}}
    assert cr.compare(cur, base_old)["verdict"] == "PASS"


def test_make_refuses_incomplete_baseline(tmp_path, monkeypatch, capsys):
    """#5：--make 有 missing 篇时拒绝写基线并非 0 退出。"""
    docs = tmp_path / "docs.txt"
    docs.write_text("h1\nh2\n", encoding="utf-8")
    baseline = tmp_path / "base.json"
    monkeypatch.setattr(cr, "run", lambda *a: {
        "docs": 2, "missing": 2,
        "total": {"questions": 0, "empty_answers": 0, "weak_questions": 0, "probe": 0},
        "per_doc": {"h1": {"missing": True}, "h2": {"missing": True}}})
    rc = cr.main(["--docs", str(docs), "--baseline", str(baseline), "--make"])
    assert rc == 2 and not baseline.exists()
    assert "不完整基线" in capsys.readouterr().err
