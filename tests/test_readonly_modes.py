"""只读 CLI 路径不得切换 SQLite journal mode 或修改数据库。"""
from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest

from mianjing_kb.kb import db


def _delete_mode_db(path):
    conn = db.connect(path)
    conn.close()
    raw = sqlite3.connect(path)
    mode = raw.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
    raw.close()
    assert mode.lower() == "delete"


def _journal_mode(path):
    raw = sqlite3.connect(path)
    try:
        return raw.execute("PRAGMA journal_mode").fetchone()[0].lower()
    finally:
        raw.close()


def _assert_still_delete(path):
    assert _journal_mode(path) == "delete"


@pytest.mark.parametrize("mode", ["stats", "plan", "dry_run"])
def test_extract_read_only_modes_do_not_switch_journal_mode(tmp_path, monkeypatch, mode):
    from mianjing_kb import extract

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    if mode in ("plan", "dry_run"):
        mapping = SimpleNamespace(by_doc={}, orphans={}, report={})
        monkeypatch.setattr(extract, "doc_pool", lambda _conn: ([], mapping))

    args = SimpleNamespace(
        db=str(path), stats=(mode == "stats"), plan=(mode == "plan"),
        dry_run=(mode == "dry_run"), require_gate=False,
        incremental=False, limit=0,
    )
    assert extract._run(args) == 0
    _assert_still_delete(path)


def test_classify_stats_uses_read_only_connection(tmp_path):
    from mianjing_kb import classify

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    args = SimpleNamespace(
        db=str(path), dry_run=False, stats=True, neighbor=False, show=0,
    )
    assert classify._run(args) == 0
    _assert_still_delete(path)


def test_classify_dry_run_uses_read_only_connection(tmp_path):
    from mianjing_kb import classify

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    args = SimpleNamespace(
        db=str(path), dry_run=True, stats=False, neighbor=False, show=0,
        status="pending_classify", redo=False, limit=0,
    )
    assert classify._run(args) == 0
    _assert_still_delete(path)


def test_bookmarks_dry_run_uses_read_only_connection(tmp_path):
    from mianjing_kb import bookmarks

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    args = SimpleNamespace(
        db=str(path), dry_run=True, only_merged=False, force=False,
    )
    assert bookmarks._run(args) == 0
    _assert_still_delete(path)


def test_ro_uri_encodes_uri_specials_and_stays_read_only(tmp_path):
    """`#`/`?` 出现在库名时，未编码的 file: URI 会改掉实际目标库或绕过 mode=ro。"""
    from mianjing_kb.kb import db

    path = tmp_path / "kb#2.sqlite"
    conn = db.connect(path)
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    uri = db.ro_uri(path)
    assert "#" not in uri
    con = sqlite3.connect(uri, uri=True)
    try:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            con.execute("INSERT INTO t VALUES (2)")
    finally:
        con.close()


# ---- 候选 #23/#30/#44/#74（P1-E）：检索/评测/巡检/对账全部只读打开 ----

def _search_db(path):
    """带 FTS 的最小检索库（DELETE journal），供只读评测用例复用。"""
    con = sqlite3.connect(str(path))
    con.executescript("""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT,
  mirror_hash TEXT, content TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
  qtype TEXT, topic TEXT, category TEXT, difficulty INTEGER, tags_json TEXT,
  position TEXT, quality TEXT, q_key TEXT);
CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('d1', 'MySQL.pdf');
INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, difficulty, q_key)
VALUES (1, 'd1', 'MySQL 索引为什么用 B+ 树', '叶子节点链表。', '八股', 'MySQL', 3, 'd1:q1');
INSERT INTO questions_fts(rowid, q_text, a_text)
VALUES (1, 'MySQL 索引为什么用 B+ 树', '叶子节点链表。');
PRAGMA journal_mode=DELETE;
""")
    con.commit()
    con.close()


def test_search_con_is_read_only(tmp_path):
    from mianjing_kb import search as S

    path = tmp_path / "search.db"
    _search_db(path)
    con = S._con(path)
    try:
        assert con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            con.execute("DELETE FROM questions")
    finally:
        con.close()
    _assert_still_delete(path)
    assert not (tmp_path / "search.db-wal").exists()


def test_eval_retrieval_does_not_mutate_delete_mode_db(tmp_path):
    from mianjing_kb import eval_retrieval as R

    path = tmp_path / "eval.db"
    _search_db(path)
    before = path.read_bytes()
    demo = R.evaluate(path, [{"q": "MySQL 索引"}], k=5, mode="kw")
    assert demo["hit_at_k"] == 1.0
    golden = R.evaluate_golden(path, [{"query": "MySQL 索引", "category": "exact",
                                       "expect_q_keys": ["d1:q1"],
                                       "relevance": [{"q_key": "d1:q1", "grade": 3}],
                                       "reviewed": True}], k=10, mode="kw")
    assert golden["recall_at_5"] == 1.0
    assert path.read_bytes() == before          # 主文件字节完全不变
    _assert_still_delete(path)
    assert not (tmp_path / "eval.db-wal").exists()
    assert not (tmp_path / "eval.db-shm").exists()


def test_ingest_queue_inspection_is_read_only(tmp_path, capsys):
    from mianjing_kb import ingest_queue

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    con = sqlite3.connect(str(path))
    con.execute("INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes,"
                " src_path, dir_path, status, batch_id, first_seen_at, last_seen_at,"
                " category, topic, collection, source_trust, parent_dir)"
                " VALUES ('a1','s','a.pdf','.pdf',1,'a.pdf','d','classified','b','now','now',"
                " '题库','JVM','c','high','d')")
    con.commit()
    con.close()
    out = tmp_path / "queue.csv"
    assert ingest_queue.main(["--db", str(path), "--stats", "--export", str(out)]) == 0
    capsys.readouterr()
    assert out.exists()
    _assert_still_delete(path)
    assert not (tmp_path / "kb.sqlite-wal").exists()


def test_reconcile_is_read_only(tmp_path, monkeypatch, capsys):
    from mianjing_kb import reconcile

    build = tmp_path / "build"
    build.mkdir()
    md_root = tmp_path / "md"
    md_root.mkdir()
    path = build / "kb.sqlite"
    _delete_mode_db(path)
    (build / "convert_log.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(reconcile, "BUILD", build)
    monkeypatch.setattr(reconcile, "MD_ROOT", md_root)
    monkeypatch.setattr(reconcile.config, "DB_PATH", path)
    assert reconcile.main() == 0
    capsys.readouterr()
    _assert_still_delete(path)
    assert not (build / "kb.sqlite-wal").exists()


def test_manifest_reconcile_and_dry_rollback_are_read_only(tmp_path, monkeypatch, capsys):
    from mianjing_kb import manifests

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    monkeypatch.setattr(manifests.config, "BUILD_DIR", tmp_path)
    assert manifests.main(["--reconcile", "--db", str(path)]) == 0
    capsys.readouterr()
    manifests.prepare("b-ro", {"tool": "canonical", "added_variants": [], "candidates": []},
                      build_dir=tmp_path)
    assert manifests.main(["--rollback", "b-ro", "--db", str(path)]) == 0
    capsys.readouterr()
    _assert_still_delete(path)
    assert not (tmp_path / "kb.sqlite-wal").exists()


def test_taxonomy_list_export_is_read_only(tmp_path, capsys):
    from mianjing_kb.kb import taxonomy

    path = tmp_path / "kb.sqlite"
    _delete_mode_db(path)
    out = tmp_path / "tax.yaml"
    assert taxonomy.main(["--db", str(path), "--export", str(out)]) == 0
    capsys.readouterr()
    _assert_still_delete(path)
    assert not (tmp_path / "kb.sqlite-wal").exists()

