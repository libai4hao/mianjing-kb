# -*- coding: utf-8 -*-
"""doc_convert / reconvert 评审回归（2026-11-19 第二轮）。"""
from __future__ import annotations

import json
import sys
import types
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from mianjing_kb import doc_convert as dc  # noqa: E402
from mianjing_kb import reconvert as rc    # noqa: E402
from kb import db, events, hashing  # noqa: E402


def _docx(path: Path, text: str = "ok") -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f"<w:document>{text}</w:document>")
    return path


class FakeDoc:
    def __init__(self, word):
        self.word = word
        self.closed = False

    def SaveAs2(self, path, FileFormat=16):
        self.word.saved_to = Path(path)
        if self.word.output == "valid":
            _docx(Path(path))
        elif self.word.output == "not_zip":
            Path(path).write_bytes(b"OLE2 fake docx")

    SaveAs = SaveAs2

    def Close(self, save=False):
        self.closed = True


class FakeWord:
    def __init__(self, output="valid"):
        self.output = output
        self.saved_to = None
        self.opened = []
        self.quit_called = 0
        self.Documents = types.SimpleNamespace(Open=self._open)

    def _open(self, path, **kw):
        self.opened.append(path)
        return FakeDoc(self)

    def Quit(self):
        self.quit_called += 1


def test_force_never_deletes_valid_same_name_docx(tmp_path):
    """--force 只针对"已有 docx 不合格"；合法同名 docx 必须原样保留。"""
    src = tmp_path / "foo.doc"
    src.write_bytes(b"old doc")
    dst = _docx(tmp_path / "foo.docx", "independent")
    before = hashing.sha256_file(dst)

    word = FakeWord()
    assert dc.convert(word, src, force=True) == (True, "exists")
    assert hashing.sha256_file(dst) == before
    assert word.opened == []                    # 连 Word 都没打开


def test_force_moves_bad_docx_aside_before_convert(tmp_path):
    src = tmp_path / "foo.doc"
    src.write_bytes(b"old doc")
    bad = tmp_path / "foo.docx"
    bad.write_bytes(b"OLE2 not zip")
    word = FakeWord()
    assert dc.convert(word, src, force=True) == (True, "converted")
    assert list(tmp_path.glob("foo.docx.bad*")), "不合格 docx 应挪成 .bad，不是删掉"
    assert dc._valid_docx(tmp_path / "foo.docx")


def test_bad_output_does_not_replace_and_cleans_tmp(tmp_path):
    src = tmp_path / "foo.doc"
    src.write_bytes(b"old doc")
    word = FakeWord(output="not_zip")
    assert dc.convert(word, src) == (False, "not_zip")
    assert not (tmp_path / "foo.docx").exists()
    assert not list(tmp_path.glob("*.tmp-*.docx"))


def test_truncated_zip_is_not_valid_docx(tmp_path):
    p = _docx(tmp_path / "a.docx")
    raw = p.read_bytes()[:60]                    # 截断：魔数还在，CRC 不完整
    p.write_bytes(raw)
    assert dc._is_zip(p)
    assert not dc._valid_docx(p)


def _fake_win32(word):
    mod = types.ModuleType("win32com.client")
    mod.DispatchEx = lambda _name: word
    pkg = types.ModuleType("win32com")
    pkg.client = mod
    return {"win32com": pkg, "win32com.client": mod}


def _mini_db(path: Path, src: Path, status="pending_classify", note=None):
    conn = db.connect(path)
    digest = hashing.sha256_file(src)
    conn.execute(
        "INSERT INTO source_documents(doc_hash, sha256, file_name, src_path, dir_path, "
        "status, note, batch_id, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (digest[:16], digest, src.name, str(src), "02-JVM", status, note, "b", "t", "t"))
    conn.commit()
    conn.close()


def _run_convert(tmp_path, monkeypatch, word, how):
    root = tmp_path / "corpus"
    (root / "02-JVM").mkdir(parents=True)
    src = root / "02-JVM" / "foo.doc"
    src.write_bytes(b"old doc")
    _docx(root / "02-JVM" / "foo.docx")
    db_path = tmp_path / "kb.sqlite"
    _mini_db(db_path, src, note="needs_convert")
    monkeypatch.setattr(dc, "convert", lambda _w, _s, _f: (how == "converted", how))
    for name, mod in _fake_win32(word).items():
        monkeypatch.setitem(sys.modules, name, mod)
    rc_code = dc.main(["--root", str(root), "--db", str(db_path), "--apply"])
    return rc_code, src, db_path


def test_exists_result_does_not_mark_superseded(tmp_path, monkeypatch):
    """同名 docx 已存在（how=exists）不能证明产物来自当前 .doc，禁止 supersede。"""
    code, src, db_path = _run_convert(tmp_path, monkeypatch, FakeWord(), "exists")
    assert code == 0
    conn = db.connect(db_path, create=False)
    row = db.get_by_hash(conn, hashing.sha256_file(src)[:16])
    assert row["status"] == "pending_classify" and row["note"] == "needs_convert"
    conn.close()


def test_converted_result_marks_superseded_with_hash_and_preserves_note(tmp_path, monkeypatch):
    code, src, db_path = _run_convert(tmp_path, monkeypatch, FakeWord(), "converted")
    assert code == 0
    conn = db.connect(db_path, create=False)
    row = db.get_by_hash(conn, hashing.sha256_file(src)[:16])
    assert row["status"] == "superseded"
    assert row["note"].startswith("converted_to:02-JVM/foo.docx#")
    assert "needs_convert" in row["note"]        # 原有 note 保留
    # T12：事件与 note 双写，读方不用再解析子串
    assert events.has(conn, row["doc_hash"], events.KIND_CONVERTED_TO)
    ev = events.rows(conn, row["doc_hash"], events.KIND_CONVERTED_TO)[0]["payload"]
    assert ev["name"] == "foo.docx" and ev["dir"] == "02-JVM" and ev["hash"]
    conn.close()


def test_reconvert_extra_rejects_ambiguous_name(tmp_path, monkeypatch):
    """--extra 同名不同目录必须按目录精确匹配；同目录多条 hash 视为歧义。"""
    monkeypatch.setattr(rc.config, "CORPUS_ROOT", tmp_path / "corpus")
    (tmp_path / "corpus" / "A").mkdir(parents=True)
    conn = db.connect(tmp_path / "kb.sqlite")
    for i in range(2):
        conn.execute(
            "INSERT INTO source_documents(doc_hash, sha256, file_name, src_path, dir_path, "
            "status, category, topic, batch_id, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (f"h{i}", "x", "x.pdf", "/A/x.pdf", "A", "classified", "题库", "综合", "b", "t", "t"))
    conn.commit()
    assert rc._extra_job(conn, "A/x.pdf") is None
    conn.close()


def test_reconvert_extra_matches_by_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(rc.config, "CORPUS_ROOT", tmp_path / "corpus")
    conn = db.connect(tmp_path / "kb.sqlite")
    conn.execute(
        "INSERT INTO source_documents(doc_hash, sha256, file_name, src_path, dir_path, "
        "status, category, topic, batch_id, first_seen_at, last_seen_at) "
        "VALUES ('h','x','x.pdf','/A/x.pdf','A','classified','题库','JVM','b','t','t')")
    conn.commit()
    job = rc._extra_job(conn, "A/x.pdf")
    assert job and job["rel"] == "A/x.pdf" and job["category"] == "题库"
    conn.close()


def test_reconvert_reads_gbk_markdown(tmp_path):
    p = tmp_path / "gbk.md"
    p.write_bytes("中文面经内容".encode("gbk"))
    assert rc._read_md(p) == "中文面经内容"


def test_reconvert_missing_dependency_message(tmp_path, monkeypatch):
    monkeypatch.setattr(rc, "convert_one", None)
    def boom():
        raise ModuleNotFoundError("No module named 'pymupdf4llm'", name="pymupdf4llm")
    monkeypatch.setattr(rc, "_load_convert_one", boom)
    with pytest.raises(RuntimeError) as ei:
        rc._require_convert_one()
    assert "依赖缺失" in str(ei.value) and "KB_CORPUS_ROOT" not in str(ei.value)


def test_reconvert_failed_job_exit_code_and_per_file_log(tmp_path, monkeypatch):
    log = tmp_path / "convert_log.json"
    log.write_text(json.dumps([
        {"rel": "A/x.pdf", "category": "题库", "topic": "综合", "ok": True},
        {"rel": "B/x.pdf", "category": "题库", "topic": "综合", "ok": True},
    ]), encoding="utf-8")
    monkeypatch.setattr(rc, "BUILD", tmp_path)
    monkeypatch.setattr(rc, "convert_one", object())
    monkeypatch.setattr(rc, "_converter_meta", lambda _cv: {})
    monkeypatch.setattr(rc, "convert_one_entry",
                        lambda *_a, **_k: {"rel": "A/x.pdf", "ok": False, "err": "boom"})
    assert rc.main([]) == 1
    assert (tmp_path / "reconvert.jsonl").exists()
    assert list(tmp_path.glob("reconvert_log-*.json"))


def test_exists_not_zip_without_force_does_not_touch_source(tmp_path):
    """不带 --force：已有非 zip 的 docx 只报 exists_not_zip，不转换、不挪动。"""
    src = tmp_path / "foo.doc"
    src.write_bytes(b"old doc")
    dst = tmp_path / "foo.docx"
    dst.write_bytes(b"OLE2 not zip")
    word = FakeWord()
    assert dc.convert(word, src, force=False) == (False, "exists_not_zip")
    assert dst.read_bytes() == b"OLE2 not zip"
    assert list(tmp_path.glob("foo.docx.bad*")) == []
