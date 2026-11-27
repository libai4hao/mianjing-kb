# -*- coding: utf-8 -*-
"""脚本侧路径边界回归（审计候选 #78/#64）：工作簿 md_rel 与样本路径解析。"""
import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load_script(name):
    spec = importlib.util.spec_from_file_location(name, REPO / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_baseline_md_rel_cannot_escape_corpus(tmp_path, monkeypatch):
    """#78：工作簿 md_rel 带 .. / 绝对路径 → 不读语料外文件（locate=notfound）。"""
    gab = _load_script("gen_answer_baseline")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "ok.md").write_text("1. 什么是 JVM？\n\n虚拟机。", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("1. 什么是 JVM？\n\n语料外答案。", encoding="utf-8")
    monkeypatch.setattr(gab, "CORPUS", corpus)
    rows = [
        {"sample_id": "s1", "md_rel": "../outside.md", "q_text": "什么是 JVM？",
         "a_error_code": None, "corrected_a_text": ""},
        {"sample_id": "s2", "md_rel": str(outside), "q_text": "什么是 JVM？",
         "a_error_code": None, "corrected_a_text": ""},
        {"sample_id": "s3", "md_rel": "ok.md", "q_text": "什么是 JVM？",
         "a_error_code": None, "corrected_a_text": ""},
    ]
    monkeypatch.setattr(gab, "load_annotations", lambda: rows)
    recs = gab.snapshot()
    assert recs[0]["locate"] == "notfound" and recs[0]["a_current"] == ""
    assert recs[1]["locate"] == "notfound" and recs[1]["a_current"] == ""
    assert recs[2]["locate"] == "found"          # 合法 rel 正常定位


def test_cut1000_md_path_returns_resolved_checked_path(tmp_path):
    """#64：_md_path 返回解析后的路径；.. 与越界仍拒绝。"""
    mc = _load_script("make_cut1000_samples")
    root = tmp_path / "md"
    (root / "a").mkdir(parents=True)
    target = root / "a" / "b.md"
    target.write_text("x", encoding="utf-8")
    got = mc._md_path(root, "a/b.md")
    assert got == target.resolve()
    with pytest.raises(ValueError):
        mc._md_path(root, "a/../../outside.md")
    with pytest.raises(ValueError):
        mc._md_path(root, "")
