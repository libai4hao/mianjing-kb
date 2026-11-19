# -*- coding: utf-8 -*-
"""ocr_merge 备份命名：扁平化不能撞名。"""
from mianjing_kb import ocr_merge as m


def test_backup_name_does_not_collapse_paths():
    assert m.backup_name("a/b_c.md", b"same") != m.backup_name("a_b/c.md", b"same")


def test_backup_name_tracks_content():
    assert m.backup_name("a/b_c.md", b"v1") != m.backup_name("a/b_c.md", b"v2")


def test_report_md_path_must_stay_under_question_md_root(tmp_path, monkeypatch):
    import pytest
    from mianjing_kb.kb import config, paths
    root = tmp_path / "20-题库MD"
    root.mkdir()
    monkeypatch.setattr(config, "QUESTION_MD_DIR", root)
    good = root / "题库" / "Redis" / "a.md"
    good.parent.mkdir(parents=True)
    good.write_text("ok", encoding="utf-8")
    assert m._trusted_md_path(good) == good
    assert m._trusted_md_path("题库/Redis/a.md") == good
    with pytest.raises(paths.UnsafePath):
        m._trusted_md_path(tmp_path / "outside.txt")
    with pytest.raises(paths.UnsafePath):
        m._trusted_md_path("../../outside.txt")


def test_merge_set_never_accepts_outside_md_from_report(tmp_path, monkeypatch):
    import json
    from mianjing_kb import ocr_pages
    from mianjing_kb.kb import config
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    pdf = corpus / "a.pdf"
    pdf.write_bytes(b"pdf placeholder")
    mdroot = tmp_path / "20-题库MD"
    mdroot.mkdir()
    ocrroot = tmp_path / "ocr_text"
    ocrroot.mkdir()
    (ocrroot / "a.pdf.txt").write_text("recognized", encoding="utf-8")
    outside = tmp_path / "victim.txt"
    outside.write_text("do not touch", encoding="utf-8")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"rows": [{"state": "fail", "rel": "a.pdf",
                                             "md": str(outside)}]}), encoding="utf-8")
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "QUESTION_MD_DIR", mdroot)
    monkeypatch.setattr(m, "OCR_TEXT", ocrroot)
    monkeypatch.setattr(ocr_pages, "is_complete_text", lambda *args: True)
    assert m.merge_set(report) == []
    assert outside.read_text(encoding="utf-8") == "do not touch"
