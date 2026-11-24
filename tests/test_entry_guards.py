# -*- coding: utf-8 -*-
"""入口守卫：build_db 禁止直跑、wlock 冲突提示带目标库、迁移预演在副本上工作。"""
from __future__ import annotations

import json
import sqlite3

import pytest

from mianjing_kb import backfill_convert, batch_label, build_db, classify, eval_cluster, extract, ingest_queue, rehearse_migrate
from mianjing_kb.kb import db, wlock


def test_build_db_refuses_direct_run(capsys):
    rc = build_db.main([])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "ABORT"
    assert "--direct" in out["note"]


def test_extract_refuses_when_kb_lock_held(tmp_path, capsys):
    db_path = tmp_path / "kb.sqlite"
    with wlock.write_lock("kb", tool="pytest", build_dir=tmp_path, target=db_path):
        rc = extract.main(["--db", str(db_path)])
    assert rc == 2
    out = json.loads(capsys.readouterr().out)
    assert out["verdict"] == "ABORT"


def test_lock_holder_message_names_target(tmp_path):
    with wlock.write_lock("online", tool="pytest", build_dir=tmp_path, target="面经库.db"):
        with pytest.raises(wlock.LockHeld, match="目标库=面经库.db"):
            with wlock.write_lock("online", tool="other", build_dir=tmp_path):
                pass


def test_rehearse_copies_and_migrates(tmp_path):
    src = tmp_path / "kb.sqlite"
    conn = db.connect(src)
    conn.execute(
        "INSERT INTO source_documents(doc_hash, sha256, file_name, src_path, batch_id, "
        "first_seen_at, last_seen_at, status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("h1", "x" * 64, "a.pdf", "/a.pdf", "b1", "t", "t", "classified"))
    conn.commit()
    conn.close()

    res = rehearse_migrate.rehearse(src, tmp_path / "work", run_plan=False)
    assert res["ok"] is True
    assert res["before"]["source_documents"] == 1
    assert res["after"]["source_documents"] == 1
    assert res["after"]["status"] == {"classified": 1}
    assert not (tmp_path / "work").exists()          # 默认删副本

    # 源库仍是只读对象：预演没有替它跑迁移
    con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    assert con.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0] == 1
    con.close()


@pytest.mark.parametrize(
    ("module", "args"),
    [
        (batch_label, ("--limit", "-1")),
        (classify, ("--limit", "-1")),
        (eval_cluster, ("--limit-md", "-1", "--skip-corpus")),
        (ingest_queue, ("--limit", "-1")),
        (backfill_convert, ("--limit", "-1")),
    ],
)
def test_limit_entrypoints_reject_negative_before_io(module, args, tmp_path, capsys):
    argv = list(args)
    if module is not backfill_convert:
        argv += ["--db", str(tmp_path / "missing.sqlite")]
    with pytest.raises(SystemExit) as exc:
        module.main(argv)
    assert exc.value.code == 2
    assert "limit" in capsys.readouterr().err


def test_limit_helpers_reject_negative_before_io(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        batch_label.pick_targets(object(), -1)
    with pytest.raises(ValueError, match="limit"):
        batch_label.review_targets(object(), -1)
    with pytest.raises(ValueError, match="limit"):
        classify.build_select_sql("pending_classify", False, -1)
    with pytest.raises(ValueError, match="limit"):
        eval_cluster.md_stats(tmp_path, -1)
    with pytest.raises(ValueError, match="limit"):
        ingest_queue.fetch(object(), -1)
    with pytest.raises(ValueError, match="limit"):
        backfill_convert.convert(-1)
