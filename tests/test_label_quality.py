# -*- coding: utf-8 -*-
"""标签质量门禁回归：碎片 / pending 未复核 / 其他主题率。"""
import sqlite3

from mianjing_kb import batch_label, label_quality


def _conn(rows):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE questions (qid INTEGER PRIMARY KEY, doc_hash TEXT, seq INTEGER,
                                q_text TEXT, a_text TEXT, q_norm_hash TEXT, topic TEXT);
        CREATE TABLE source_documents (doc_hash TEXT, topic TEXT, file_name TEXT);
        CREATE TABLE labels_staging (qid INTEGER PRIMARY KEY, topic TEXT, qtype TEXT,
                                     difficulty INTEGER, review_state TEXT);
    """)
    for qid, text, topic, state in rows:
        conn.execute("INSERT INTO questions(qid, doc_hash, seq, q_text, topic) "
                     "VALUES(?,?,?,?,NULL)", (qid, f"doc-{qid}", qid, text))
        conn.execute("INSERT INTO labels_staging(qid, topic, qtype, difficulty, review_state) "
                     "VALUES(?,?, '八股', 2, ?)", (qid, topic, state))
    return conn


def test_quality_pass():
    rows = [(i, f"什么是第{i}个知识点?", "后端", "agree") for i in range(1, 21)]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "PASS" and q["ok"] is True


def test_quality_empty_does_not_block_orphan_cleanup():
    """全是孤儿行时 JOIN 为空，门禁应 SKIP 放行，交给 merge 的 --drop-orphans。"""
    q = label_quality.assess(_conn([]))
    assert q["state"] == "SKIP" and q["ok"] is True


def test_quality_blocks_code_fragment():
    rows = [(1, "#include <stdio.h>", "C++", "agree")] + [
        (i, f"什么是第{i}个知识点?", "后端", "agree") for i in range(2, 11)]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "FAIL"
    assert any("code_fragment" in f for f in q["fails"])
    assert q["detail"]["fragment_qids"] == [1]


def test_quality_blocks_pending():
    rows = [(i, f"什么是第{i}个知识点?", "后端", "pending" if i == 3 else "agree")
            for i in range(1, 11)]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "FAIL"
    assert any("pending_review" in f for f in q["fails"])


def test_quality_ignores_disagree_rows():
    """disagree 行已单独 hold，不应因它自己的碎片/主题问题阻断其余行 merge。"""
    rows = [(1, "#include <stdio.h>", "C++", "disagree"),
            (2, "什么是虚函数?", "C++", "agree")]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "PASS" and q["ok"] is True
    assert q["detail"]["staged"] == 1


def test_quality_other_topic_hard_and_warn():
    hard = [(i, f"什么是第{i}个知识点?", "其他" if i <= 6 else "后端", "agree")
            for i in range(1, 21)]
    assert label_quality.assess(_conn(hard))["state"] == "FAIL"  # 30%
    warn = [(i, f"什么是第{i}个知识点?", "其他" if i <= 4 else "后端", "agree")
            for i in range(1, 21)]
    q = label_quality.assess(_conn(warn))                        # 20%
    assert q["state"] == "WARN" and q["ok"] is True


def test_quality_strict_blocks_small_all_other_batch():
    rows = [(i, f"什么是第{i}个知识点?", "其他", "agree") for i in range(1, 4)]
    assert label_quality.assess(_conn(rows))["ok"] is True
    strict = label_quality.assess(_conn(rows), strict=True)
    assert strict["state"] == "FAIL" and strict["ok"] is False
    assert any("other_topic_rate" in f for f in strict["fails"])


def test_pick_targets_skips_code_fragments():
    conn = _conn([(1, "#include <stdio.h>", "C++", "agree"),
                  (2, "什么是虚函数?", "C++", "agree"),
                  (3, "int top = 0 ;", "C++", "agree")])
    conn.execute("UPDATE questions SET topic='未分类'")
    stats: dict = {}
    got = [r["qid"] for r in batch_label.pick_targets(conn, skip_code=True, stats=stats)]
    assert got == [2]
    assert stats["skipped_code"] == 2
    assert [r["qid"] for r in stats["skipped_code_rows"]] == [1, 3]
    got2 = [r["qid"] for r in batch_label.pick_targets(conn, skip_code=False)]
    assert got2 == [1, 2, 3]


def test_quality_blocks_structural_hold():
    rows = [(1, "假如有实习经验,他会问你实习中做过的项目", "其他", "agree")] + [
        (i, f"什么是第{i}个知识点?", "后端", "agree") for i in range(2, 11)]
    q = label_quality.assess(_conn(rows))
    assert q["state"] == "FAIL"
    assert any("quality_hold" in f for f in q["fails"])
    assert q["detail"]["quality_hold_qids"] == [1]


def test_pick_targets_skips_structural_hold_but_keeps_normal_short_question():
    conn = _conn([(1, "假如有实习经验,他会问你实习中做过的项目", "其他", "agree"),
                  (2, "int 类型占几个字节", "C++", "agree"),
                  (3, "什么是虚函数?", "C++", "agree")])
    conn.execute("UPDATE questions SET topic='未分类'")
    stats: dict = {}
    got = [r["qid"] for r in batch_label.pick_targets(conn, skip_code=True, stats=stats)]
    assert got == [2, 3]
    assert [r["qid"] for r in stats["skipped_code_rows"]] == [1]
    assert stats["skipped_code_rows"][0]["hold_reason"] == "残缺"


def _conn_with_tags(rows):
    """带 tags 列的 staging（标签侧 WARN 用；rows: qid, text, topic, tags, state）。"""
    conn = _conn([])
    conn.execute("ALTER TABLE labels_staging ADD COLUMN tags TEXT")
    for qid, text, topic, tags, state in rows:
        conn.execute("INSERT INTO questions(qid, doc_hash, seq, q_text, topic) "
                     "VALUES(?,?,?,?,NULL)", (qid, f"doc-{qid}", qid, text))
        conn.execute("INSERT INTO labels_staging(qid, topic, qtype, difficulty, tags, review_state) "
                     "VALUES(?,?, '八股', 2, ?, ?)", (qid, topic, tags, state))
    return conn


def test_quality_warns_low_vocab_hit():
    """词表命中率 0（全是自由标签）→ WARN，不 FAIL（开放词表，低命中只提示词表/prompt 失效）。"""
    rows = [(i, f"什么是第{i}个知识点?", "后端", '["随便写的词一","随便写的词二"]', "agree")
            for i in range(1, 21)]
    q = label_quality.assess(_conn_with_tags(rows))
    assert q["state"] == "WARN" and q["ok"] is True
    assert any("tag_vocab_hit" in w for w in q["detail"]["warns"])
    assert q["detail"]["tag_metrics"]["vocab_hit_rate"] == 0.0


class _FetchmanyCursor:
    def __init__(self, cursor):
        self._cursor = cursor

    def fetchmany(self, size):
        return self._cursor.fetchmany(size)

    def fetchall(self):
        raise AssertionError("pick_targets 不得一次性取回全部候选")


class _FetchmanyConnection:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=()):
        return _FetchmanyCursor(self._conn.execute(sql, params))


def test_pick_targets_streams_rows_and_preserves_all_skip_stats():
    conn = _conn([(1, "什么是虚函数?", "后端", "agree"),
                  (2, "#include <stdio.h>", "C++", "agree"),
                  (3, "什么是索引?", "后端", "agree"),
                  (4, "int top = 0 ;", "C++", "agree")])
    conn.execute("UPDATE questions SET topic='未分类'")
    stats = {}
    got = batch_label.pick_targets(_FetchmanyConnection(conn), limit=1, stats=stats)
    assert [r["qid"] for r in got] == [1]
    assert stats["skipped_code"] == 2
    assert [r["qid"] for r in stats["skipped_code_rows"]] == [2, 4]


def test_pick_targets_without_filter_uses_bounded_limit_query():
    conn = _conn([(1, "什么是虚函数?", "后端", "agree"),
                  (2, "什么是索引?", "后端", "agree")])
    conn.execute("UPDATE questions SET topic='未分类'")
    got = batch_label.pick_targets(_FetchmanyConnection(conn), limit=1, skip_code=False)
    assert [r["qid"] for r in got] == [1]


def test_quality_warns_dimension_tags_backflow():
    """tags 里出现维度词（topic 取值）→ 维度重复率 WARN，说明 stopwords 又漏了。"""
    rows = [(i, f"什么是第{i}个知识点?", "后端",
             '["其他","索引"]' if i == 1 else '["索引"]', "agree")
            for i in range(1, 21)]
    q = label_quality.assess(_conn_with_tags(rows))
    assert q["state"] == "WARN" and "tag_dimension_dup" in " ".join(q["detail"]["warns"])
    assert q["detail"]["tag_metrics"]["dimension_dup_rate"] > 0.02
