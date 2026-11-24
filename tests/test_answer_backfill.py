# -*- coding: utf-8 -*-
"""补答题（迁移 019）：目标筛选、写回留痕、拒答/失败分流。"""
import pytest
from pathlib import Path

from mianjing_kb import answer_backfill as ab
from mianjing_kb.kb import config, db, wlock


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    c = db.connect(tmp_path / "kb.sqlite")
    rows = [
        (1, "什么是 JVM 类加载？", "", "boundary_gap"),
        (2, "Redis 为什么快？", "", "ok"),
        (3, "目录伪题", "", "toc_noise"),
        (4, "短", "", "boundary_gap"),
        (5, "已补过的题", "已有答案", "boundary_gap"),
    ]
    for qid, q, a, st in rows:
        c.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, answer_state, "
                  "q_norm, q_norm_hash, simhash, span_start, span_end, extracted_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, f"h{qid}", qid, q, a, st, f"n{qid}", f"h{qid}", 0, 0, 1, "now"))
    c.commit()
    yield c
    c.close()


def test_pick_targets_skips_toc_noise_answered_and_marked(conn):
    got = [q for q, _ in ab.pick_targets(conn)]
    assert got == [1, 4, 2]       # 3 目录伪题、5 已有答案被排除；4 太短留给 run 里筛


def test_run_writes_answer_with_provenance(conn, monkeypatch):
    def fake_chat(cfg, system, user):
        if "Redis" in user:
            return {"text": "NO_ANSWER"}
        return {"text": "类加载分五个阶段。"}
    monkeypatch.setattr(ab.llm_client, "chat", fake_chat)

    res = ab.run(conn, [(1, "什么是 JVM 类加载？"), (2, "Redis 为什么快？")], concurrency=1)
    assert res["answered"] == 1 and res["refused"] == 1
    row = conn.execute("SELECT a_text, answer_source, answer_model FROM questions WHERE qid=1").fetchone()
    assert row["a_text"] == "类加载分五个阶段。" and row["answer_source"] == "llm" and row["answer_model"]
    refused = conn.execute("SELECT a_text, answer_source FROM questions WHERE qid=2").fetchone()
    assert refused["a_text"] == "" and refused["answer_source"] == "llm_refused"


def test_run_dry_run_does_not_write(conn, monkeypatch):
    monkeypatch.setattr(ab.llm_client, "chat", lambda *a, **k: {"text": "答案"})
    ab.run(conn, [(1, "什么是 JVM 类加载？")], concurrency=1, dry_run=True)
    row = conn.execute("SELECT a_text FROM questions WHERE qid=1").fetchone()
    assert row["a_text"] == ""


def test_run_does_not_overwrite_answer_filled_after_pick(conn, monkeypatch):
    """P1-2（2026-11-25 review）：选题与写回之间答案被别的工具补上（如 qsalvage 拆分），
    模型答案不得覆盖它，也不改写 answer_source。"""
    monkeypatch.setattr(ab.llm_client, "chat", lambda *a, **k: {"text": "模型答案"})
    targets = ab.pick_targets(conn)
    conn.execute("UPDATE questions SET a_text='原文拆出的答案' WHERE qid=1")
    conn.commit()
    res = ab.run(conn, targets, concurrency=1)
    row = conn.execute("SELECT a_text, answer_source FROM questions WHERE qid=1").fetchone()
    assert row["a_text"] == "原文拆出的答案" and row["answer_source"] == ""
    assert res["answered"] == 1 and res["written"] == 1
    assert res["guard_skipped"] == 1 and res["error"] == 1


def tmp_file(conn):
    """fixture 的库文件：从已打开的连接上问出来，避免测试自己再拼路径。"""
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def test_preview_defaults_to_small_limit_and_rejects_negative(conn, monkeypatch):
    """不写库的预览不写库、不迁移；--limit -1 直接拒绝（SQL 的 LIMIT -1 等于不限制）。"""
    calls = []
    monkeypatch.setattr(ab.llm_client, "chat", lambda *a, **k: calls.append(1) or {"text": "答案"})
    db_file = tmp_file(conn)
    assert ab.main(["--db", str(db_file), "--limit", "1"]) == 0
    assert len(calls) == 1
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE answer_source<>''").fetchone()[0] == 0
    with pytest.raises(SystemExit) as exc:
        ab.main(["--db", str(db_file), "--limit", "-1"])
    assert exc.value.code == 2


def test_dry_run_limit_applies_only_when_limit_omitted(conn, monkeypatch):
    """省略 --limit 的预览只调 DRY_RUN_LIMIT 条；显式 --limit 0 才全量预览。"""
    calls = []
    monkeypatch.setattr(ab.llm_client, "chat", lambda *a, **k: calls.append(1) or {"text": "答案"})
    for i in range(100, 130):
        conn.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, answer_state, "
                     "q_norm, q_norm_hash, simhash, span_start, span_end, extracted_at) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                     (i, f"h{i}", i, f"足够长的问题{i}是什么", "", "boundary_gap", f"n{i}",
                      f"h{i}", 0, 0, 1, "now"))
    conn.commit()
    db_file = tmp_file(conn)
    assert ab.main(["--db", str(db_file)]) == 0
    assert len(calls) == ab.DRY_RUN_LIMIT - 1                 # 20 条目标里 1 条太短不送模型
    assert ab.main(["--db", str(db_file), "--limit", "0"]) == 0
    assert len(calls) == (ab.DRY_RUN_LIMIT - 1) + 32          # 全量 33 条目标，同样 1 条太短
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE answer_source<>''").fetchone()[0] == 0


def test_stats_reports_remaining(conn):
    s = ab.stats(conn)
    assert s["todo"] == 3                 # 1/4/2 仍待补；3 目录伪题不计
    assert s["empty_by_source"]["(原文空)"] >= 4


def test_run_does_not_persist_llm_error(conn, monkeypatch):
    """P1-2：单条调用失败不能把题记成终态，否则一次掉线就永久跳过（code review 2026-11-24）。"""
    def boom(*a, **k):
        raise RuntimeError("URLError: connection refused")
    monkeypatch.setattr(ab.llm_client, "chat", boom)

    res = ab.run(conn, [(1, "什么是 JVM 类加载？")], concurrency=1)
    assert res["error"] == 1
    row = conn.execute("SELECT a_text, answer_source FROM questions WHERE qid=1").fetchone()
    assert row["answer_source"] == "" and row["a_text"] == ""
    assert [q for q, _ in ab.pick_targets(conn)][0] == 1      # 仍可被下一轮选中


def test_pick_targets_retries_legacy_llm_error(conn):
    """历史遗留 llm_error 行（旧版本写进去的）也要能重试。"""
    conn.execute("UPDATE questions SET answer_source='llm_error' WHERE qid=2")
    conn.commit()
    assert 2 in [q for q, _ in ab.pick_targets(conn)]


def test_pick_targets_rejects_negative_limit(conn):
    with pytest.raises(ValueError, match="大于等于 0"):
        ab.pick_targets(conn, -1)


def test_apply_holds_custom_db_lock_during_selection_and_model_call(tmp_path, monkeypatch, capsys):
    build = tmp_path / "custom-build"
    build.mkdir()
    path = build / "kb.sqlite"
    monkeypatch.setattr(config, "BUILD_DIR", tmp_path / "other-build")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "other-build" / "kb.sqlite")
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, answer_state, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at) "
        "VALUES ('doc',1,'什么是 JVM？','','boundary_gap','jvm','h1',0,0,1,'now')")
    conn.commit()
    conn.close()

    seen = []
    real_pick = ab.pick_targets

    def observe(name, fn):
        def wrapper(*args, **kwargs):
            locked, holder = wlock.inspect_lock(build / "locks" / "kb.lock")
            seen.append((name, locked, (holder or {}).get("tool")))
            return fn(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(ab, "pick_targets", observe("pick", real_pick))
    monkeypatch.setattr(ab.llm_client, "chat",
                        observe("chat", lambda *a, **k: {"text": "模型答案"}))
    assert ab.main(["--db", str(path), "--apply", "--concurrency", "1"]) == 0
    capsys.readouterr()
    assert seen == [(name, True, "answer_backfill") for name in ("pick", "chat")]
    conn = db.connect(path, create=False, readonly=True)
    assert conn.execute("SELECT a_text FROM questions").fetchone()[0] == "模型答案"
    conn.close()


@pytest.mark.parametrize("mutation", ["delete", "question", "source"])
def test_run_does_not_count_stale_target_as_answered(conn, monkeypatch, mutation):
    monkeypatch.setattr(ab.llm_client, "chat", lambda *a, **k: {"text": "模型答案"})
    if mutation == "delete":
        conn.execute("DELETE FROM questions WHERE qid=1")
    elif mutation == "question":
        conn.execute("UPDATE questions SET q_text='已经变化的题干' WHERE qid=1")
    else:
        conn.execute("UPDATE questions SET answer_source='llm_refused' WHERE qid=1")
    conn.commit()

    res = ab.run(conn, [(1, "什么是 JVM 类加载？")], concurrency=1)

    assert res["answered"] == 0 and res["written"] == 0
    assert res["guard_skipped"] == 1 and res["error"] == 1


@pytest.mark.parametrize("extra", [[], ["--stats"], ["--stats", "--apply"]])
def test_read_only_modes_do_not_acquire_write_lock(conn, monkeypatch, extra):
    monkeypatch.setattr(ab.llm_client, "chat", lambda *a, **k: {"text": "答案"})

    def forbidden(*args, **kwargs):
        raise AssertionError("read-only mode took write lock")

    monkeypatch.setattr(ab.wlock, "write_lock", forbidden)
    assert ab.main(["--db", str(tmp_file(conn)), *extra]) == 0
