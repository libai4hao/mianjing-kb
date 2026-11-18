# -*- coding: utf-8 -*-
"""privacy_scan 规则与豁免标注的单元测试。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import privacy_scan as ps  # noqa: E402


def _hits(tmp_path, monkeypatch, rel, text):
    monkeypatch.setattr(ps, "REPO", tmp_path)
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return ps.scan([rel])


def test_abs_path_and_secret_are_flagged(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "a.py", 'P = r"D:\\Project\\x"\nKEY = "sk-' + "a" * 24 + '"\n')
    rules = {h["rule"] for h in hits}
    assert "abs_path" in rules and "secret" in rules


def test_allow_marker_skips_line(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "b.py",
                 'phone = "13812345678"  # privacy-scan: allow 合成\n')
    assert hits == []


def test_noreply_github_mail_is_fine(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "c.md", "作者 <libai4hao@users.noreply.github.com>\n")
    assert hits == []


def test_placeholder_is_fine(tmp_path, monkeypatch):
    hits = _hits(tmp_path, monkeypatch, "d.md", "语料根：<CORPUS_ROOT>\\20-题库MD\n")
    assert hits == []
