"""归一器迁移：先快照、再同事务改 questions + label_facts。"""
from __future__ import annotations

import hashlib
import json

from mianjing_kb.kb import db
from mianjing_kb.migrate_norm import run


def _q(conn, seq, text, answer="a", old_norm=None):
    norm = old_norm if old_norm is not None else text.lower().replace("+", "")
    h = hashlib.sha256(norm.encode()).hexdigest()[:16]
    conn.execute(
        "INSERT INTO questions(doc_hash,seq,q_text,a_text,span_start,span_end,q_norm,"
        "q_norm_hash,simhash,extracted_at,q_key) VALUES(?,?,?,?,0,1,?,?,?,?,?)",
        ("doc", seq, text, answer, norm, h, 0, "t", f"doc:{h}"),
    )
    return h


def test_dry_run_reports_changes_without_writing(tmp_path):
    p = tmp_path / "kb.sqlite"
    conn = db.connect(p)
    old = _q(conn, 1, "i++")
    conn.commit()
    conn.close()

    out = tmp_path / "snap.json"
    r = run(p, apply=False, out=out)
    assert r["dry_run"] is True
    assert r["questions_changed"] == 1
    assert r["old_to_new_count"] == 1
    assert out.exists()
    snap = json.loads(out.read_text(encoding="utf-8"))
    assert snap["old_to_new"][old] != old

    conn = db.connect(p, readonly=True)
    row = conn.execute("SELECT q_norm_hash FROM questions").fetchone()
    assert row["q_norm_hash"] == old
    conn.close()


def test_apply_rewrites_questions_and_label_facts(tmp_path):
    p = tmp_path / "kb.sqlite"
    conn = db.connect(p)
    old = _q(conn, 1, "i++")
    conn.execute(
        "INSERT INTO label_facts(q_norm_hash,topic,qtype,difficulty,tags,"
        "normalizer_version,q_norm_sample,source,labeled_at,confidence,merged_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (old, "C++", "八股", "中", "指针", "nfkc-clean-v1", "i", "model",
         "2026-01-01", 0.9, "2026-01-01"),
    )
    conn.commit()
    conn.close()

    r = run(p, apply=True, out=tmp_path / "snap.json")
    assert r["questions_changed"] == 1
    assert r["labels_rewritten"] == 1

    conn = db.connect(p, readonly=True)
    q = conn.execute("SELECT q_norm,q_norm_hash,q_key FROM questions").fetchone()
    assert q["q_norm"] == "ipostinc"
    assert q["q_key"] == f"doc:{q['q_norm_hash']}"
    lab = conn.execute("SELECT * FROM label_facts").fetchone()
    assert lab["q_norm_hash"] == q["q_norm_hash"]
    assert lab["normalizer_version"] == "nfkc-clean-v2"
    assert lab["q_norm_sample"] == "ipostinc"
    assert lab["topic"] == "C++"
    conn.close()


def test_collision_keeps_longest_answer_and_drops_other(tmp_path):
    p = tmp_path / "kb.sqlite"
    conn = db.connect(p)
    # 旧口径把兼容汉字 ㈱ 直接删掉（归一为 ""），与真正的「株」不同 hash；
    # NFKC 后两者都归一为「株」，落到同一新 hash。
    _q(conn, 1, "㈱", "short", old_norm="")
    _q(conn, 2, "株", "a much longer answer", old_norm="株")
    conn.commit()
    conn.close()

    r = run(p, apply=True, out=tmp_path / "snap.json")
    assert r["questions_dropped"] == 1

    conn = db.connect(p, readonly=True)
    rows = conn.execute("SELECT a_text,seq FROM questions").fetchall()
    assert len(rows) == 1
    assert rows[0]["a_text"] == "a much longer answer"
    assert rows[0]["seq"] == 2
    conn.close()
