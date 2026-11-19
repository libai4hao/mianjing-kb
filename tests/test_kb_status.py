# -*- coding: utf-8 -*-
"""T31 `kb status` 测试：只读汇总、门禁过期判定、缺库不崩。"""
import json

import pytest

from mianjing_kb import status as ks
from mianjing_kb.kb import config, db as kbdb


@pytest.fixture()
def mini_env(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")

    con = kbdb.connect(build / "kb.sqlite")
    con.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, topic) "
        "VALUES ('h1', ?, '甲.pdf', '.pdf', 1, 'x/甲.pdf', '02', 'processed', 't', 'now', 'now', 'JVM')",
        ("h1" + "0" * 48,))
    con.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at) "
        "VALUES (1, 'h1', 1, '什么是JVM？', '答案', '八股', 'high', '什么是jvm', 'x', 0, 0, 1, 'now')")
    con.execute(
        "INSERT INTO document_products (doc_hash, text_source, questions_n, extracted_at, updated_at) "
        "VALUES ('h1', 'md', 0, NULL, 'now')")
    con.execute(
        "INSERT INTO labels_staging (qid, topic, qtype, difficulty, tags, labeled_at, review_state) "
        "VALUES (1, 'JVM', '八股', 3, '[]', 'now', 'pending')")
    con.commit()
    con.close()
    yield build


def test_collect_reports_pipeline_counts(mini_env):
    data = ks.collect()
    assert data["kb"]["source"]["total"] == 1
    assert data["kb"]["source"]["ingested"] == 1
    assert data["kb"]["products"]["not_extracted"] == 1
    assert data["kb"]["pipeline"]["pending_extract"] == 1
    assert data["kb"]["questions"]["missing_topic"] == 1
    assert data["kb"]["labels"]["review_pending"] == 1
    assert data["gates"]["state"] == "MISSING"
    assert any("门禁" in x for x in data["attention"])


def test_gate_report_stale_when_db_changes(mini_env):
    build = mini_env
    report = {
        "summary": {"at": "2026-11-19T10:00:00", "verdict": "PASS", "hard_fail": 0,
                    "gates": {"G1": "PASS"},
                    "db_identity": {"kb": {"path": "x", "size_bytes": 1, "mtime_ns": 1},
                                    "out": {"path": "y", "missing": True}}},
        "gates_detail": [],
    }
    (build / "verify_report.json").write_text(json.dumps(report), encoding="utf-8")
    data = ks.collect()
    assert data["gates"]["state"] == "STALE"
    assert any("kb" in r for r in data["gates"]["stale_reasons"])


def test_missing_kb_does_not_crash(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    data = ks.collect()
    assert data["kb"] == {"present": False}
    assert data["retrieval"]["present"] is False
    assert "kb.sqlite 不存在" in data["attention"]


def test_render_text_contains_sections(mini_env):
    text = ks.render_text(ks.collect())
    for section in ("[源]", "[处理]", "[题]", "[打标]", "[检索库]", "[门禁]"):
        assert section in text
