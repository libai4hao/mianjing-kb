# -*- coding: utf-8 -*-
"""D-34：refresh 的 G9 Golden Query 软门状态。"""
from mianjing_kb import eval_retrieval as ER
from mianjing_kb import refresh as rf


def test_g9_warns_until_golden_queries_are_reviewed(tmp_path, monkeypatch):
    monkeypatch.setattr(ER, "evaluate_golden", lambda *a, **k: {
        "queries": 50, "recall_at_5": 0.2, "reviewed_count": 2})
    gate = rf.g9_golden_queries(tmp_path / "shadow.db")
    assert gate["id"] == "G9" and gate["state"] == "WARN"
    assert gate["level"] == "软"


def test_g9_fails_when_reviewed_recall_drops(tmp_path, monkeypatch):
    monkeypatch.setattr(ER, "evaluate_golden", lambda *a, **k: {
        "queries": 50, "recall_at_5": 0.05, "reviewed_count": 50})
    gate = rf.g9_golden_queries(tmp_path / "shadow.db")
    assert gate["state"] == "FAIL"


def test_g9_passes_when_reviewed_recall_meets_baseline(tmp_path, monkeypatch):
    monkeypatch.setattr(ER, "evaluate_golden", lambda *a, **k: {
        "queries": 50, "recall_at_5": 0.2, "reviewed_count": 50})
    gate = rf.g9_golden_queries(tmp_path / "shadow.db")
    assert gate["state"] == "PASS"


def test_g9_fails_when_evaluator_raises(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db boom")

    monkeypatch.setattr(ER, "evaluate_golden", boom)
    gate = rf.g9_golden_queries(tmp_path / "shadow.db")
    assert gate["state"] == "FAIL" and "db boom" in gate["fails"][0]
