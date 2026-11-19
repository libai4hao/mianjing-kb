# -*- coding: utf-8 -*-
"""P1 5,000 题试跑准备：副本裁剪、MD 拷贝与报告。"""
import json
import sqlite3
from pathlib import Path

import pytest

from mianjing_kb import pilot5000 as P


def _mk_prod(tmp_path: Path) -> tuple[Path, Path]:
    prod = tmp_path / "prod"
    md = prod / "20-题库MD"
    (md / "a").mkdir(parents=True)
    (md / "a" / "one.md").write_text("题一", encoding="utf-8")
    (md / "a" / "two.md").write_text("题二", encoding="utf-8")
    db = prod / "kb.sqlite"
    con = sqlite3.connect(str(db))
    con.executescript("""
CREATE TABLE schema_version(version TEXT, applied_at TEXT);
INSERT INTO schema_version VALUES('017_label_facts_hardening','now');
CREATE TABLE source_documents(doc_hash TEXT PRIMARY KEY, status TEXT, gate_result TEXT, src_path TEXT);
CREATE TABLE document_products(doc_hash TEXT PRIMARY KEY, md_rel TEXT,
  extracted_at TEXT, indexed_at TEXT, questions_n INTEGER);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_key TEXT, q_text TEXT);
CREATE TABLE question_variants(variant_id INTEGER PRIMARY KEY, canonical_id TEXT, q_key TEXT);
CREATE TABLE canonical_dependents(canonical_id TEXT, q_key TEXT);
CREATE TABLE merge_candidates(q_key TEXT);
CREATE TABLE canonical_questions(canonical_id TEXT);
CREATE TABLE labels_staging(q_norm_hash TEXT);
CREATE TABLE label_facts(q_norm_hash TEXT);
CREATE TABLE golden_labels(q_norm_hash TEXT);
CREATE TABLE document_events(doc_hash TEXT);
INSERT INTO source_documents VALUES('h1','processed',NULL,NULL),('h2','processed',NULL,NULL),('h3','processed',NULL,NULL);
INSERT INTO document_products VALUES('h1','a/one.md','x','x',1),('h2','a/two.md','x','x',1);
INSERT INTO questions VALUES(1,'h1','h1:n1','题一'),(2,'h2','h2:n2','题二'),(3,'h3','h3:n3','题三');
INSERT INTO question_variants VALUES(1,'c1','h1:n1'),(2,'c2','h2:n2'),(3,'c3','h3:n3');
INSERT INTO canonical_dependents VALUES('c1','h1:n1'),('c2','h2:n2'),('c3','h3:n3');
INSERT INTO canonical_questions VALUES('c1'),('c2'),('c3');
INSERT INTO labels_staging VALUES('x');
INSERT INTO label_facts VALUES('x');
INSERT INTO golden_labels VALUES('x');
INSERT INTO document_events VALUES('h1');
""")
    con.commit()
    con.close()
    return db, md


def test_prepare_copies_and_trims_without_touching_prod(tmp_path, monkeypatch):
    prod_db, prod_md = _mk_prod(tmp_path)
    monkeypatch.setattr("mianjing_kb.extract.extract_doc_status", lambda h, p: ([{}], "ok"))
    cfg = {
        "prod_db": str(prod_db),
        "prod_md_dir": str(prod_md),
        "pilot_root": str(tmp_path / "pilot"),
        "docs": 2,
        "seed": 7,
        "target_min": 2,
        "target_max": 100,
    }
    out = P.prepare(cfg)
    assert out["docs"] == 2 and out["target_questions"] == 2
    assert out["md_copied"] == 2 and out["md_missing"] == 0
    pilot_db = Path(cfg["pilot_root"]) / "build" / "kb.sqlite"
    con = sqlite3.connect(str(pilot_db))
    try:
        assert con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM document_products").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM label_facts").fetchone()[0] == 0
    finally:
        con.close()
    assert (prod_db).exists()
    # 生产库未被裁剪
    con = sqlite3.connect(f"file:{prod_db.as_posix()}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 3
    con.close()


def test_report_writes_json(tmp_path, monkeypatch):
    prod_db, prod_md = _mk_prod(tmp_path)
    monkeypatch.setattr("mianjing_kb.extract.extract_doc_status", lambda h, p: ([{}], "ok"))
    cfg = {
        "prod_db": str(prod_db), "prod_md_dir": str(prod_md),
        "pilot_root": str(tmp_path / "pilot"), "docs": 2, "seed": 7,
        "target_min": 2, "target_max": 100,
    }
    P.prepare(cfg)
    out = P.report(cfg)
    assert out["pilot_schema_version"] == "017_label_facts_hardening"
    assert out["sample"]["actual_questions"] == 0   # prepare 只裁剪，extract 后才产生题
    path = Path(cfg["pilot_root"]) / "build" / "pilot_5000_report.json"
    assert json.loads(path.read_text(encoding="utf-8"))["integrity"]["qkey_dup"] == 0


def test_label_requires_key(tmp_path, monkeypatch):
    prod_db, prod_md = _mk_prod(tmp_path)
    monkeypatch.setattr("mianjing_kb.extract.extract_doc_status", lambda h, p: ([{}], "ok"))
    cfg = {
        "prod_db": str(prod_db), "prod_md_dir": str(prod_md),
        "pilot_root": str(tmp_path / "pilot"), "docs": 2, "seed": 7,
        "target_min": 2, "target_max": 100, "llm": {"key": ""},
    }
    P.prepare(cfg)
    with pytest.raises(RuntimeError, match="llm.key"):
        P.label(cfg)


def test_main_returns_nonzero_when_label_fails(tmp_path, monkeypatch):
    """子进程失败不能伪装成 rc=0；pilot 入口必须把 stopped_on_failure 传出去。"""
    monkeypatch.setattr(P, "ensure_config", lambda _p: {"pilot_root": str(tmp_path)})
    monkeypatch.setattr(P, "label", lambda _cfg, dry_run=False: {
        "stages": [{"stage": "label_write", "rc": 1}], "stopped_on_failure": True})
    assert P.main(["label", "--config", str(tmp_path / "c.json")]) == 1


def test_label_writes_to_pilot_db_by_default(tmp_path, monkeypatch):
    """默认必须写试跑库 staging；--dry-run 才只试输出。"""
    root = tmp_path / "pilot"
    (root / "build").mkdir(parents=True)
    (root / "build" / "kb.sqlite").write_bytes(b"")
    cfg = {
        "pilot_root": str(root), "repo_root": str(tmp_path), "python": "py",
        "llm": {"key": "k", "provider": "online", "base": "http://x",
                "model": "m", "price": "1,4", "rpm": 5},
    }
    seen = []

    def fake_run(cmd, env, log, cwd):
        seen.append(cmd)
        return {"cmd": cmd, "rc": 0, "seconds": 0.1}

    monkeypatch.setattr(P, "_run", fake_run)
    P.label(cfg)
    assert "--dry-run" not in seen[-1]
    assert seen[-1][-2:] == ["--limit", "100"]
    P.label(cfg, dry_run=True)
    assert "--dry-run" in seen[-1]





