# -*- coding: utf-8 -*-
"""answer_state：源文档缺答案 / 切块缺口 / 目录噪声 的判定口径。"""
import sqlite3

from mianjing_kb.answer_state import GAP, NO_SOURCE, OK, TOC, summary, classify


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("create table questions (qid integer primary key, doc_hash text, seq integer,"
                 " q_text text, a_text text)")
    conn.executemany("insert into questions (qid,doc_hash,seq,q_text,a_text) values (?,?,?,?,?)",
                     rows)
    conn.execute("create table questionsx (x integer)")  # 占位，避免空库误判
    return conn


class TestClassify:
    def test_answerless_doc_is_source_missing(self):
        # 5 题里只有 1 题有答案 → 覆盖率 0.2，属于"问题清单"文档
        conn = _db([(1, "d1", 1, "什么是 A？", ""), (2, "d1", 2, "什么是 B？", ""),
                    (3, "d1", 3, "什么是 C？", ""), (4, "d1", 4, "什么是 D？", ""),
                    (5, "d1", 5, "什么是 E？", "有答案")])
        res = classify(conn)
        assert res["state"][1] == NO_SOURCE and res["state"][5] == OK

    def test_single_gap_in_good_doc(self):
        # 6 题里 5 题有答案（覆盖率 0.83）→ 那条空的算切块缺口
        rows = [(i, "d2", i, f"问题{i}？", "答案") for i in range(1, 6)]
        rows.append((6, "d2", 6, "问题6？", ""))
        res = classify(_db(rows))
        assert res["state"][6] == GAP
        assert res["counts"].get(GAP) == 1

    def test_toc_run_marks_noise(self):
        # 10 条连续空答案、标题式（无问词、短）→ toc_noise
        titles = ["基础语法", "数据类型", "运算符", "关键字", "变量与方法",
                  "面向对象", "集合", "异常", "IO 流", "反射"]
        rows = [(i, "d3", i, t, "") for i, t in enumerate(titles, 1)]
        rows += [(11, "d3", 11, "什么是 JVM 内存模型？", "答案")]
        res = classify(_db(rows))
        assert res["counts"].get(TOC, 0) >= 9

    def test_threshold_configurable(self):
        rows = [(i, "d4", i, f"问题{i}？", "" if i > 3 else "答案") for i in range(1, 7)]
        strict = classify(_db(rows), doc_coverage_min=0.9)   # 覆盖率 0.5 ≤ 0.9 → 全算源文档缺答案
        loose = classify(_db(rows), doc_coverage_min=0.3)    # 覆盖率 0.5 > 0.3 → 算切块缺口
        assert strict["counts"].get(NO_SOURCE, 0) == 3
        assert loose["counts"].get(GAP, 0) == 3


class TestSummary:
    def test_summary_counts(self):
        conn = _db([(1, "d", 1, "q", "a")])
        conn.execute("alter table questions add column answer_state text")
        conn.execute("update questions set answer_state='ok'")
        assert summary(conn)["counts"] == {"ok": 1}


class TestOnlyMissing:
    def test_only_missing_keeps_historical_states(self, tmp_path, monkeypatch):
        """生产历史状态（含 qsalvage 修正）不能被全库重算改写；--only-missing 只补空值。"""
        import sqlite3

        from mianjing_kb import answer_state
        from mianjing_kb.kb import config, db as kbdb

        monkeypatch.setattr(config, "BUILD_DIR", tmp_path / "build")
        (tmp_path / "build").mkdir()
        p = tmp_path / "kb.sqlite"
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        kbdb.migrate(conn)
        conn.execute(
            "insert into questions (qid, doc_hash, seq, q_text, a_text, span_start, span_end, "
            "q_norm, q_norm_hash, simhash, extracted_at, answer_state) "
            "values (1,'d',1,'有答案的题','答案',0,1,'有答案的题','h1',0,'now','toc_noise')")   # 历史状态与重算口径不一致
        conn.execute(
            "insert into questions (qid, doc_hash, seq, q_text, a_text, span_start, span_end, "
            "q_norm, q_norm_hash, simhash, extracted_at) "
            "values (2,'d',2,'空答案题','',0,1,'空答案题','h2',0,'now')")                      # 未判，应被补上
        conn.commit()
        conn.close()

        assert answer_state.main(["--db", str(p), "--only-missing", "--apply"]) == 0
        c = sqlite3.connect(str(p))
        rows = dict(c.execute("select qid, answer_state from questions").fetchall())
        c.close()
        assert rows[1] == "toc_noise"
        assert rows[2] in ("boundary_gap", "no_answer_source", "toc_noise")


class TestPreviewIsReadOnly:
    def test_preview_does_not_migrate_or_write(self, tmp_path, monkeypatch):
        """P1-1（2026-11-25 review）：旧实现预览也走 db.migrate()，对停在旧 schema 的回滚快照
        "看一眼分布"就把它升到最新。预览必须只读；--apply 才允许迁移并写库。"""
        import sqlite3

        from mianjing_kb import answer_state
        from mianjing_kb.kb import config, db as kbdb

        monkeypatch.setattr(config, "BUILD_DIR", tmp_path)
        p = tmp_path / "snapshot.db"
        conn = sqlite3.connect(str(p))
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE schema_version (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
        conn.commit()
        for version, _sql in kbdb.MIGRATIONS:
            if version < "018":
                kbdb._apply_one(conn, version, _sql)
        conn.execute("INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
                     "q_norm, q_norm_hash, simhash, extracted_at) "
                     "VALUES ('d', 1, '足够长的一道面试题是什么', '', 0, 1, 'n', 'h', 0, 'now')")
        conn.commit()
        conn.close()
        before = p.read_bytes()

        assert answer_state.main(["--db", str(p)]) == 0
        assert p.read_bytes() == before
        c = sqlite3.connect(str(p))
        cols = [r[1] for r in c.execute("PRAGMA table_info(questions)")]
        assert "answer_state" not in cols
        assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] < len(kbdb.MIGRATIONS)
        c.close()

        # --apply：迁移落在写锁内，状态补上
        assert answer_state.main(["--db", str(p), "--apply"]) == 0
        c = sqlite3.connect(str(p))
        assert c.execute("SELECT answer_state FROM questions").fetchone()[0] in (
            "boundary_gap", "no_answer_source", "toc_noise")
        assert c.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == len(kbdb.MIGRATIONS)
        c.close()


def test_only_missing_preview_accepts_legacy_schema_without_state_column(tmp_path, capsys):
    import json

    from mianjing_kb import answer_state
    from mianjing_kb.kb import db as kbdb

    path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE schema_version (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    for version, _sql in kbdb.MIGRATIONS:
        if version < "018":
            kbdb._apply_one(conn, version, _sql)
    conn.execute("INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
                 "q_norm, q_norm_hash, simhash, extracted_at) "
                 "VALUES ('d', 1, '足够长的一道面试题是什么', '', 0, 1, 'n', 'h', 0, 'now')")
    conn.commit()
    conn.close()
    before = path.read_bytes()

    assert answer_state.main(["--db", str(path), "--only-missing"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["subset"]["total"] == 1
    assert out["database"]["total"] == 1
    assert out["database"]["nonempty_by_state"] == {}
    assert path.read_bytes() == before


def test_only_missing_reports_subset_and_database_scopes(tmp_path, monkeypatch, capsys):
    import json
    from mianjing_kb import answer_state
    from mianjing_kb.kb import config, db

    monkeypatch.setattr(config, "BUILD_DIR", tmp_path)
    path = tmp_path / "kb.sqlite"
    conn = db.connect(path)
    rows = [
        (1, "d1", 1, "有答案一", "答案一", "no_answer_source"),
        (2, "d1", 2, "有答案二", "答案二", "boundary_gap"),
        (3, "d1", 3, "空答案一", "", None),
        (4, "d1", 4, "空答案二", "", "toc_noise"),
        (5, "d2", 1, "另一文档空答案", "", None),
        (6, "d1", 5, "有答案但未判状态", "答案三", None),
    ]
    conn.executemany(
        "INSERT INTO questions (qid,doc_hash,seq,q_text,a_text,q_norm,q_norm_hash,"
        "simhash,span_start,span_end,extracted_at,answer_state) VALUES (?,?,?,?,?,?,?,0,0,1,'now',?)",
        [(qid, dh, seq, q, a, q, f"h{qid}", state) for qid, dh, seq, q, a, state in rows])
    conn.commit()
    conn.close()

    assert answer_state.main(["--db", str(path), "--only-missing"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["scope"]["selection"] == "missing_states_only"
    assert out["subset"] == {"total": 3, "empty": 2,
                             "counts": {answer_state.NO_SOURCE: 1,
                                        answer_state.GAP: 1, answer_state.OK: 1}}
    assert out["database"]["total"] == 6
    assert out["database"]["empty"] == 3
    assert out["database"]["nonempty_by_state"] == {
        answer_state.NO_SOURCE: 1, answer_state.GAP: 1, "missing": 1}
    assert out["database"]["docs_with_empty"] == 2
    assert out["total"] == 3 and out["empty"] == 2
    assert out["docs_with_empty"] == 2


def test_only_missing_apply_keeps_nonempty_historical_states(tmp_path, monkeypatch, capsys):
    import json
    from mianjing_kb import answer_state
    from mianjing_kb.kb import config, db

    monkeypatch.setattr(config, "BUILD_DIR", tmp_path)
    path = tmp_path / "kb.sqlite"
    conn = db.connect(path)
    conn.executemany(
        "INSERT INTO questions (qid,doc_hash,seq,q_text,a_text,q_norm,q_norm_hash,"
        "simhash,span_start,span_end,extracted_at,answer_state) VALUES (?,?,?,?,?,?,?,0,0,1,'now',?)",
        [(1, "d", 1, "有答案", "源答案", "有答案", "h1", "boundary_gap"),
         (2, "d", 2, "空答案", "", "空答案", "h2", None)])
    conn.commit()
    conn.close()

    assert answer_state.main(["--db", str(path), "--only-missing", "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["database"]["nonempty_by_state"] == {answer_state.GAP: 1}
    conn = sqlite3.connect(str(path))
    rows = dict(conn.execute("SELECT qid, answer_state FROM questions"))
    conn.close()
    assert rows[1] == answer_state.GAP
    assert rows[2] == answer_state.NO_SOURCE


def test_apply_holds_custom_db_lock_for_migrate_classify_and_backfill(tmp_path, monkeypatch, capsys):
    from mianjing_kb import answer_state
    from mianjing_kb.kb import config, db, wlock

    build = tmp_path / "configured-build"
    build.mkdir()
    custom = tmp_path / "custom-build"
    custom.mkdir()
    path = custom / "kb.sqlite"
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    conn = db.connect(path)
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, q_norm, q_norm_hash, "
        "simhash, span_start, span_end, extracted_at) "
        "VALUES ('doc',1,'什么是 JVM？','答案','jvm','h1',0,0,1,'now')")
    conn.commit()
    conn.close()

    seen = []
    real_migrate, real_classify, real_backfill = db.migrate, answer_state.classify, answer_state.backfill

    def checked(name, fn):
        def wrapper(*args, **kwargs):
            locked, holder = wlock.inspect_lock(custom / "locks" / "kb.lock")
            seen.append((name, locked, (holder or {}).get("tool")))
            return fn(*args, **kwargs)
        return wrapper

    monkeypatch.setattr(answer_state.db, "migrate", checked("migrate", real_migrate))
    monkeypatch.setattr(answer_state, "classify", checked("classify", real_classify))
    monkeypatch.setattr(answer_state, "backfill", checked("backfill", real_backfill))

    assert answer_state.main(["--db", str(path), "--apply"]) == 0
    capsys.readouterr()
    assert seen == [(name, True, "answer_state") for name in ("migrate", "classify", "backfill")]


def test_preview_never_acquires_write_lock(tmp_path, monkeypatch):
    from mianjing_kb import answer_state
    from mianjing_kb.kb import db

    path = tmp_path / "kb.sqlite"
    db.connect(path).close()
    monkeypatch.setattr(answer_state.wlock, "write_lock",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("preview took write lock")))
    assert answer_state.main(["--db", str(path)]) == 0
