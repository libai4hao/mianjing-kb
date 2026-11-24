# -*- coding: utf-8 -*-
"""qsalvage：题答同行拆分 / 目录行判定 / 目录区连续串识别 / 空白答案归一。"""
import csv
import json
import sqlite3
from pathlib import Path

from mianjing_kb import qsalvage
from mianjing_kb.kb import config, db as kbdb
from mianjing_kb.qsalvage import (
    blank_whitespace_answers,
    split_inline_answer,
    toc_row_reason,
    toc_runs,
)


class TestSplitInlineAnswer:
    def test_docx_single_line_question_and_answer(self):
        q = "什么是 MyBatis？答：MyBatis 是一个可以自定义 SQL、存储过程和高级映射的持久层框架。"
        got = split_inline_answer(q)
        assert got is not None
        head, tail = got
        assert head == "什么是 MyBatis？"
        assert tail.startswith("MyBatis 是一个")

    def test_answer_key_variant(self):
        got = split_inline_answer("HashMap 的扩容机制？答案: 负载因子 0.75，容量翻倍。")
        assert got is not None
        assert got[0] == "HashMap 的扩容机制？"

    def test_marker_at_head_is_not_split(self):
        # 「答：…」打头说明这一行本来只有答案，题干在上面，不能据此造题
        assert split_inline_answer("答：配置文件设置 usegeneratedkeys 为 true 31、在 mapper 中如何传递多个参数？") is None

    def test_plain_question_untouched(self):
        assert split_inline_answer("什么是CAS，它有什么特性2") is None
        assert split_inline_answer("synchronized 和 Lock 的区别？") is None

    def test_requires_ask_shape_before_marker(self):
        assert split_inline_answer("事务回滚机制答：要么全做要么全不做") is None


class TestTocRowReason:
    def test_dot_leader_with_page(self):
        assert toc_row_reason("服务续约 ...... 54") == "dot_leader_page"
        assert toc_row_reason("Spring-Core ........…………... ..……………………… ..4") == "dot_leader_page"
        assert toc_row_reason("JAVA 基础........... . ...................................................") is None

    def test_code_and_sentence_not_toc(self):
        # 省略号出现在代码题里，没有行尾页码 → 不是目录
        assert toc_row_reason("sizeof(p1...p4) =?") is None
        assert toc_row_reason("其他 balabala...") is None

    def test_long_line_not_toc(self):
        q = "什么是字节码？采用字节码的大好处是什么" + "…" * 40 + " 12"
        assert toc_row_reason(q) is None


class TestBlankWhitespaceAnswers:
    def test_whitespace_only_answer_becomes_null(self):
        # SQLite 的 trim() 默认不去换行，两种空答案口径曾差 20 条（docs/44）
        conn = sqlite3.connect(":memory:")
        conn.execute("create table questions (qid integer primary key, a_text text)")
        conn.executemany("insert into questions (qid,a_text) values (?,?)",
                         [(1, "\n\n"), (2, "  \t\n "), (3, "有答案"), (4, None), (5, "")])
        assert blank_whitespace_answers(conn) == 3
        left = dict(conn.execute("select qid, a_text from questions"))
        assert left[1] == "" and left[2] == "" and left[5] == "" and left[3] == "有答案"


class TestTocRuns:
    def _rows(self, qs):
        return [(i + 1, q, "") for i, q in enumerate(qs)]

    def test_title_run_detected(self):
        rows = self._rows(["基础语法", "数据类型", "运算符", "关键字", "变量与方法",
                           "面向对象", "集合", "异常", "IO 流"])
        runs = toc_runs(rows, min_len=8)
        assert len(runs) == 1 and len(runs[0]) == 9

    def test_question_run_not_flagged(self):
        rows = self._rows(["什么是 Java？", "如何创建线程？", "为什么用 B+ 树？", "JVM 内存怎么划分？",
                           "GC 有哪几种？", "索引失效的原因有哪些？", "什么是 CAS？", "简述 AQS 原理"])
        assert toc_runs(rows, min_len=8) == []

    def test_short_run_below_min_len(self):
        rows = self._rows(["基础语法", "数据类型", "运算符"])
        assert toc_runs(rows, min_len=8) == []

    def test_run_breaks_on_answered_block(self):
        rows = [(1, "基础语法", ""), (2, "数据类型", ""), (3, "运算符", ""), (4, "关键字", ""),
                (5, "变量与方法", "局部变量在栈上分配"), (6, "面向对象", ""), (7, "集合", ""),
                (8, "异常", ""), (9, "IO 流", "")]
        assert toc_runs(rows, min_len=8) == []


def _main_db(tmp_path, monkeypatch):
    configured = tmp_path / "configured-build"
    configured.mkdir()
    monkeypatch.setattr(config, "BUILD_DIR", configured)
    path = tmp_path / "custom" / "kb.sqlite"
    conn = kbdb.connect(path)
    long_q = "什么是数据库索引的原理" + "很长的题干内容" * 20 + "？答：" + "索引用于加速查询。"
    rows = [
        (1, "d", 1, long_q, "", long_q, "h1", "d:h1"),
        (2, "d", 2, "服务续约 ...... 54", "", "服务续约 ...... 54", "h2", "d:h2"),
    ]
    conn.executemany(
        "INSERT INTO questions (qid,doc_hash,seq,q_text,a_text,q_norm,q_norm_hash,q_key,"
        "simhash,span_start,span_end,extracted_at) VALUES (?,?,?,?,?,?,?,?,0,0,1,'now')", rows)
    conn.commit()
    conn.close()
    return path, long_q


def test_main_isolates_preview_and_apply_artifacts_and_keeps_full_text(tmp_path, monkeypatch, capsys):
    path, long_q = _main_db(tmp_path, monkeypatch)
    preview = tmp_path / "explicit-preview"
    assert qsalvage.main(["--db", str(path), "--out", str(preview)]) == 0
    capsys.readouterr()
    with (preview / "split.csv").open(encoding="utf-8-sig", newline="") as f:
        split = next(csv.DictReader(f))
    assert split["old_q"] == long_q and len(split["old_q"]) > 160
    before_preview = (preview / "split.csv").read_bytes()

    assert qsalvage.main(["--db", str(path), "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    apply_dir = Path(out["out_dir"])
    assert apply_dir.parent == path.parent / "qsalvage"
    assert apply_dir.name.startswith("apply-")
    assert Path(out["backup"]).is_file()
    assert (preview / "split.csv").read_bytes() == before_preview
    with (apply_dir / "retire.csv").open(encoding="utf-8-sig", newline="") as f:
        retired = next(csv.DictReader(f))
    assert retired["q"] == "服务续约 ...... 54"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    assert conn.execute("SELECT a_text FROM questions").fetchone()[0] == "索引用于加速查询。"
    conn.close()

    assert qsalvage.main(["--db", str(path)]) == 0
    second = json.loads(capsys.readouterr().out)
    assert Path(second["out_dir"]).name.startswith("preview-")
    assert Path(second["out_dir"]) != apply_dir


def test_main_rejects_nonempty_explicit_output_before_apply(tmp_path, monkeypatch, capsys):
    path, long_q = _main_db(tmp_path, monkeypatch)
    out_dir = tmp_path / "occupied"
    out_dir.mkdir()
    (out_dir / "keep.txt").write_text("不可覆盖", encoding="utf-8")

    assert qsalvage.main(["--db", str(path), "--apply", "--out", str(out_dir)]) == 2
    out = json.loads(capsys.readouterr().out)
    assert "非空" in out["error"]
    assert (out_dir / "keep.txt").read_text(encoding="utf-8") == "不可覆盖"
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT q_text FROM questions WHERE qid=1").fetchone()[0] == long_q
    conn.close()


def test_plan_treats_existing_merge_candidate_as_split_conflict():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, seq INTEGER,
          q_text TEXT, a_text TEXT, q_key TEXT, qtype TEXT);
        CREATE TABLE merge_candidates(q_key TEXT PRIMARY KEY, canonical_id TEXT, status TEXT);
        INSERT INTO questions VALUES(1,'d',1,'什么是 MyBatis？答：MyBatis 是一个可以自定义 SQL 的持久层框架。','','d:old','八股');
        """)
    first = qsalvage.plan(conn)["split"][0]
    conn.execute("INSERT INTO merge_candidates VALUES(?,?,?)",
                 (first["new_q_key"], "manual", "approved"))
    conn.commit()
    split = qsalvage.plan(conn)["split"][0]
    assert split["conflict"] is True
    conn.close()


def test_apply_syncs_candidates_and_scopes_canonical_cleanup(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, seq INTEGER,
          q_text TEXT, a_text TEXT, q_norm TEXT, q_norm_hash TEXT, simhash INTEGER,
          q_key TEXT, qtype TEXT);
        CREATE TABLE canonical_questions(canonical_id TEXT PRIMARY KEY, witness INTEGER, updated_at TEXT);
        CREATE TABLE canonical_dependents(canonical_id TEXT, q_key TEXT, doc_hash TEXT, collection TEXT,
          PRIMARY KEY(canonical_id, q_key));
        CREATE TABLE question_variants(variant_id INTEGER PRIMARY KEY, canonical_id TEXT,
          q_key TEXT, doc_hash TEXT, answering_text TEXT);
        CREATE TABLE merge_candidates(q_key TEXT PRIMARY KEY, canonical_id TEXT, status TEXT);
        INSERT INTO questions VALUES
          (1,'gone',1,'什么是 MyBatis？答：MyBatis 是一个可以自定义 SQL 的持久层框架。','','old','h1',0,'gone:old','八股'),
          (2,'gone',2,'服务续约 ...... 54','','old','h2',0,'gone:retire1','八股'),
          (3,'gone',3,'其他目录 ...... 12','','old','h5',0,'gone:retire2','八股'),
          (4,'keep',1,'什么是 JVM？','Java 虚拟机','jvm','h3',0,'keep:h3','八股'),
          (5,'keep',2,'什么是 Redis？','内存数据库','redis','h4',0,'keep:h4','八股');
        INSERT INTO canonical_questions VALUES
          ('split-survivor',9,'old'),('retire-only',9,'old'),('shared-survivor',9,'old'),
          ('unrelated-orphan',9,'old'),('variant-only',9,'old'),('candidate-only',9,'old');
        INSERT INTO canonical_dependents VALUES
          ('split-survivor','gone:old','gone','c1'),
          ('retire-only','gone:retire1','gone','c1'),
          ('shared-survivor','gone:retire2','gone','c1'),
          ('shared-survivor','keep:h3','keep','c2');
        INSERT INTO question_variants VALUES
          (1,'split-survivor','gone:old','gone',''),
          (2,'retire-only','gone:retire1','gone',''),
          (3,'shared-survivor','gone:retire2','gone',''),
          (4,'variant-only','keep:h4','keep','内存数据库');
        INSERT INTO merge_candidates VALUES
          ('gone:old','split-survivor','approved'),('gone:retire1','retire-only','pending'),
          ('keep:h3','shared-survivor','approved'),('manual:h9','candidate-only','rejected');
        """)
    plan = qsalvage.plan(conn)
    out = qsalvage.apply(conn, plan, tmp_path / "out")
    new_key = plan["split"][0]["new_q_key"]
    assert out["split_applied"] == 1 and out["retired"] == 2
    assert out["merge_candidates_moved"] == 1
    assert out["merge_candidates_removed"] == 1
    assert out["canonical_questions_removed"] == 1
    assert conn.execute("SELECT q_key FROM merge_candidates WHERE canonical_id='split-survivor'"
                        ).fetchall() == [(new_key,)]
    assert conn.execute("SELECT status FROM merge_candidates WHERE q_key='manual:h9'"
                        ).fetchone()[0] == "rejected"
    assert conn.execute("SELECT canonical_id FROM canonical_questions ORDER BY 1").fetchall() == [
        ("candidate-only",), ("shared-survivor",), ("split-survivor",),
        ("unrelated-orphan",), ("variant-only",)]
    assert conn.execute("SELECT witness FROM canonical_questions WHERE canonical_id='shared-survivor'"
                        ).fetchone()[0] == 1
    assert conn.execute("SELECT answering_text FROM question_variants WHERE variant_id=1"
                        ).fetchone()[0].startswith("MyBatis 是一个")
    conn.close()


def test_main_preview_is_readonly_and_apply_locks_target_parent(tmp_path, monkeypatch, capsys):
    path, _long_q = _main_db(tmp_path, monkeypatch)
    real_lock = qsalvage.wlock.write_lock
    real_backup = kbdb.backup_db

    def forbidden(*_args, **_kwargs):
        raise AssertionError("预览不应拿写锁或做备份")

    monkeypatch.setattr(qsalvage.wlock, "write_lock", forbidden)
    monkeypatch.setattr(qsalvage.db, "backup_db", forbidden)
    assert qsalvage.main(["--db", str(path)]) == 0
    capsys.readouterr()
    assert not (config.BUILD_DIR / "locks" / "kb.lock").exists()

    monkeypatch.setattr(qsalvage.wlock, "write_lock", real_lock)
    seen = []

    def checked_backup(db_path, *args, **kwargs):
        locked, holder = qsalvage.wlock.inspect_lock(path.parent / "locks" / "kb.lock")
        seen.append((locked, (holder or {}).get("tool")))
        return real_backup(db_path, *args, **kwargs)

    monkeypatch.setattr(qsalvage.db, "backup_db", checked_backup)
    assert qsalvage.main(["--db", str(path), "--apply"]) == 0
    capsys.readouterr()
    assert seen == [(True, "qsalvage")]
    assert not (config.BUILD_DIR / "locks" / "kb.lock").exists()
    assert (path.parent / "locks" / "kb.lock").exists()
