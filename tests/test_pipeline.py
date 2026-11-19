# -*- coding: utf-8 -*-
"""pipeline 失败传播：任一阶段失败必须停止并返回 1，不能继续跑后续阶段。"""
from mianjing_kb import pipeline


def _root(tmp_path):
    root = tmp_path / "inbox"
    root.mkdir()
    return root


def test_first_stage_failure_stops_pipeline(tmp_path, monkeypatch):
    calls = []

    def fake_run(args):
        calls.append(args)
        return None

    monkeypatch.setattr(pipeline, "run", fake_run)
    assert pipeline.main(["--root", str(_root(tmp_path)), "--db", str(tmp_path / "kb.sqlite")]) == 1
    assert len(calls) == 1


def test_second_stage_failure_stops_pipeline(tmp_path, monkeypatch):
    calls = []

    def fake_run(args):
        calls.append(args)
        return {"actions": {}} if len(calls) == 1 else None

    monkeypatch.setattr(pipeline, "run", fake_run)
    assert pipeline.main(["--root", str(_root(tmp_path)), "--db", str(tmp_path / "kb.sqlite")]) == 1
    assert len(calls) == 2


def test_all_stages_success_returns_zero(tmp_path, monkeypatch):
    calls = []

    def fake_run(args):
        calls.append(args)
        return {"actions": {}, "ok": True}

    monkeypatch.setattr(pipeline, "run", fake_run)
    assert pipeline.main(["--root", str(_root(tmp_path)), "--db", str(tmp_path / "kb.sqlite")]) == 0
    assert len(calls) == 3


def test_check_mode_also_propagates_intake_failure(tmp_path, monkeypatch):
    calls = []

    def fake_run(args):
        calls.append(args)
        return None

    monkeypatch.setattr(pipeline, "run", fake_run)
    assert pipeline.main(["--root", str(_root(tmp_path)), "--db", str(tmp_path / "kb.sqlite"),
                          "--check"]) == 1
    assert len(calls) == 1
