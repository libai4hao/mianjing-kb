# -*- coding: utf-8 -*-
"""canonical 落库测试：簇→两表映射、topic 多数票、witness、幂等重建、--dry-run。"""
import json
import sqlite3

import pytest
from mianjing_kb import canonical as cn  # noqa: E402
from mianjing_kb.kb import config, db as kbdb  # noqa: E402

REAL_META = config.META_DIR

N_JVM = "什么是jvm内存模型"          # 归一化后（normalize 去标点小写）
N_MYSQL = "什么是mysql索引"
N_REDIS = "redis持久化机制"


@pytest.fixture()
def mini_db(tmp_path, monkeypatch):
    """kb.sqlite：3 簇（JVM×2docs / MySQL×3docs / Redis 单件）+ 对应 source_documents。"""
    build = tmp_path / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    conn = sqlite3.connect(build / "kb.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript((REAL_META / "schema" / "001_init.sql").read_text(encoding="utf-8"))
    conn.executescript((REAL_META / "schema" / "004_questions.sql").read_text(encoding="utf-8"))
    docs = [("h1", "甲.pdf", "JVM", "c1"), ("h2", "乙.pdf", "JVM", "c2"),
            ("h3", "丙.pdf", "MySQL", "c3"), ("h4", "丁.pdf", "MySQL", "c4"),
            ("h5", "戊.pdf", "Redis", "c5")]
    for h, name, topic, col in docs:
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
            "dir_path, status, batch_id, first_seen_at, last_seen_at, topic, collection) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, h + "0" * 48, name, ".pdf", 1, f"x/{name}", "02", "classified", "t",
             "now", "now", topic, col))
    # 5 道题：X=JVM 簇(甲,乙)；Y=MySQL 簇(乙,丙,丁)；Z=Redis 单件(戊)
    rows = [
        ("h1", 1, N_JVM), ("h2", 1, N_JVM),
        ("h2", 2, N_MYSQL), ("h3", 1, N_MYSQL), ("h4", 1, N_MYSQL),
        ("h5", 1, N_REDIS),
    ]
    for i, (h, seq, norm) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
            "q_norm_hash, simhash, span_start, span_end, extracted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (i, h, seq, norm, f"答案{i}", "八股", "high", norm, norm[:16], 0, 0, 1, "now"))
    kbdb._ensure_stable_question_keys(conn)   # T29：测试库补 q_key/依赖表（与正式迁移同函数）
    conn.commit()
    yield conn
    conn.close()


def test_build_and_counts(mini_db):
    assert cn.main([]) == 0
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 3
    # 6 变体 = X 簇 2（甲,乙）+ Y 簇 3（乙,丙,丁）+ Z 单件 1（戊）
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 6


def test_witness_and_topic_vote(mini_db):
    cn.main([])
    rows = {r["title_norm"]: r for r in mini_db.execute(
        "SELECT * FROM canonical_questions").fetchall()}
    assert rows[N_JVM]["witness"] == 2 and rows[N_JVM]["topic"] == "JVM"
    assert rows[N_MYSQL]["witness"] == 3 and rows[N_MYSQL]["topic"] == "MySQL"
    assert rows[N_REDIS]["witness"] == 1 and rows[N_REDIS]["topic"] == "Redis"
    # variant 的 collection 归属与 canonical_id 级联
    var = mini_db.execute("SELECT * FROM question_variants "
                          "WHERE doc_hash='h1' AND question_no='1'").fetchone()
    assert var["variant_id"] == cn.variant_id(var["q_key"])
    assert var["collection"] == "c1"
    assert var["canonical_id"] == rows[N_JVM]["canonical_id"]


def test_variant_id_stable_when_seq_shifts(mini_db):
    """variant_id 只跟 q_key 走：文档前部增删题导致 seq 移位，变体身份不变。"""
    cn.main([])
    qkey = mini_db.execute(
        "SELECT q_key FROM questions WHERE doc_hash='h1' AND seq=1").fetchone()[0]
    before = mini_db.execute("SELECT variant_id FROM question_variants WHERE q_key=?",
                             (qkey,)).fetchone()[0]
    assert before == cn.variant_id(qkey)
    mini_db.execute("UPDATE questions SET seq=2 WHERE doc_hash='h1' AND seq=1")
    mini_db.commit()
    cn.main([])
    after = mini_db.execute("SELECT variant_id FROM question_variants WHERE q_key=?",
                            (qkey,)).fetchone()[0]
    assert after == before


def test_prune_orphan_variants(mini_db, capsys):
    """题被删掉后，其 q_key 的旧变体与 dependent 必须清理，不能留下脏见证。"""
    cn.main([])
    qkey = mini_db.execute(
        "SELECT q_key FROM questions WHERE doc_hash='h5' AND seq=1").fetchone()[0]
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants WHERE q_key=?",
                           (qkey,)).fetchone()[0] == 1
    mini_db.execute("DELETE FROM questions WHERE doc_hash='h5' AND seq=1")
    mini_db.commit()
    capsys.readouterr()
    assert cn.main(["--incremental", "--batch-id", "t-prune"]) == 0
    capsys.readouterr()
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants WHERE q_key=?",
                           (qkey,)).fetchone()[0] == 0
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_dependents WHERE q_key=?",
                           (qkey,)).fetchone()[0] == 0


def test_witness_uses_distinct_collection(mini_db):
    """Q-2：见证数 = distinct collection——同 collection 的多篇只算 1 个来源。"""
    mini_db.execute("UPDATE source_documents SET collection='c1' WHERE doc_hash IN ('h1','h2')")
    mini_db.execute("UPDATE source_documents SET collection='c9' WHERE doc_hash IN ('h3','h4')")
    mini_db.commit()
    cn.main([])
    rows = {r["title_norm"]: r for r in mini_db.execute(
        "SELECT * FROM canonical_questions").fetchall()}
    assert rows[N_JVM]["witness"] == 1          # 甲、乙同 c1 → 1 个来源
    assert rows[N_MYSQL]["witness"] == 2        # 乙(c1) + 丙/丁(c9) → 2 个来源


def test_witness_falls_back_to_doc_hash_when_collection_empty(mini_db):
    mini_db.execute("UPDATE source_documents SET collection=NULL")
    mini_db.commit()
    cn.main([])
    rows = {r["title_norm"]: r for r in mini_db.execute(
        "SELECT * FROM canonical_questions").fetchall()}
    assert rows[N_JVM]["witness"] == 2          # 空 collection 不能合成 1 个


def test_idempotent_and_dry_run(mini_db):
    cn.main([])
    cn.main([])                                    # 重建幂等：不翻倍
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 3
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 6
    # dry-run 不写
    mini_db.execute("DELETE FROM canonical_questions")
    mini_db.execute("DELETE FROM question_variants")
    mini_db.commit()
    cn.main(["--dry-run"])
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 0


def test_incremental_attach_create_and_manifest_rollback(mini_db, tmp_path):
    """T29：同题吸附、新题建簇，都只写增量；manifest apply 回滚到原状。"""
    from mianjing_kb import manifests
    cn.main([])
    # 新增两题：h6 与 JVM 同 norm（吸附），h7 全新（建簇）
    mini_db.executemany(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, topic, collection) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [("h6", "h6" + "0" * 48, "己.pdf", ".pdf", 1, "x/己.pdf", "02", "classified", "t",
          "now", "now", "JVM", "c6"),
         ("h7", "h7" + "0" * 48, "庚.pdf", ".pdf", 1, "x/庚.pdf", "02", "classified", "t",
          "now", "now", "JVM", "c7")])
    mini_db.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at, q_key) "
        "VALUES (7,'h6',1,?,'答案7','八股','high',?,?,0,0,1,'now',?)",
        (N_JVM, N_JVM, N_JVM[:16], f"h6:{N_JVM[:16]}"))
    mini_db.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at, q_key) "
        "VALUES (8,'h7',1,?,'答案8','八股','high',?,?,0,0,1,'now',?)",
        ("什么是垃圾回收", "什么是垃圾回收", "gc" * 8, "h7:" + "gc" * 8))
    mini_db.commit()
    assert cn.main(["--incremental", "--batch-id", "t1"]) == 0
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 4
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 8
    jvm = mini_db.execute(
        "SELECT canonical_id,witness FROM canonical_questions WHERE title_norm=?", (N_JVM,)).fetchone()
    assert jvm["witness"] == 3                     # c1/c2/c6 三个 collection
    mf = manifests.load("t1", tmp_path / "build")
    assert mf and mf["status"] == "committed"
    assert len(mf["created_canonicals"]) == 1 and len(mf["attached"]) == 1

    out = manifests.rollback(mini_db, "t1", apply=True, build_dir=tmp_path / "build")
    assert out["ok"] and out["status"] == "rolled_back"
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_questions").fetchone()[0] == 3
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == 6


def test_truncated_qnorm_prefix_collision_does_not_merge(mini_db):
    """#4：500 字前缀相同但全文 hash 不同的长题不得凭截断前缀精确吸附。"""
    cn.main([])
    prefix = "长题" * 260                    # 520 字 > TITLE_MAX=500
    assert len(prefix) > cn.TITLE_MAX
    stored = prefix[:cn.TITLE_MAX]          # extract 落库口径：q_norm 截断 500
    _add_source_and_question(mini_db, "hA", 1, stored, "hA:hashA", collection="cA")
    mini_db.execute("UPDATE questions SET q_norm_hash='hashA' WHERE doc_hash='hA'")
    mini_db.commit()
    assert cn.main(["--incremental", "--batch-id", "t-long1"]) == 0
    _add_source_and_question(mini_db, "hB", 1, stored, "hB:hashB", collection="cB")
    mini_db.execute("UPDATE questions SET q_norm_hash='hashB' WHERE doc_hash='hB'")
    mini_db.commit()
    assert cn.main(["--incremental", "--batch-id", "t-long2"]) == 0
    # 两条长题必须各自成簇（全文不同），不得吸附进同一 canonical
    ca = mini_db.execute("SELECT canonical_id FROM question_variants WHERE q_key='hA:hashA'").fetchone()[0]
    cb = mini_db.execute("SELECT canonical_id FROM question_variants WHERE q_key='hB:hashB'").fetchone()[0]
    assert ca != cb
    # 对照：全文 hash 相同的真同题仍精确吸附
    _add_source_and_question(mini_db, "hC", 1, stored, "hC:hashA", collection="cC")
    mini_db.execute("UPDATE questions SET q_norm_hash='hashA' WHERE doc_hash='hC'")
    mini_db.commit()
    assert cn.main(["--incremental", "--batch-id", "t-long3"]) == 0
    cc = mini_db.execute("SELECT canonical_id FROM question_variants WHERE q_key='hC:hashA'").fetchone()[0]
    assert cc == ca


def _add_source_and_question(conn, h, seq, norm, qkey, topic="JVM", collection="c9"):
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at, topic, collection) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, h + "0" * 48, f"{h}.pdf", ".pdf", 1, f"x/{h}.pdf", "02", "classified", "t",
         "now", "now", topic, collection))
    sh = __import__("mianjing_kb.question_freq", fromlist=["simhash"]).simhash(norm)
    sh = sh if sh < 2 ** 63 else sh - 2 ** 64
    conn.execute(
        "INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
        "q_norm_hash, simhash, span_start, span_end, extracted_at, q_key) "
        "VALUES ((SELECT COALESCE(MAX(qid),0)+1 FROM questions),?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (h, seq, norm, f"答案-{h}", "八股", "high", norm, norm[:16],
         sh, 0, 1, "now", qkey))
    conn.commit()


def test_singleton_count_is_member_count(mini_db, capsys):
    cn.main([])
    out = json.loads(capsys.readouterr().out)
    assert out["singletons"] == 1                 # 只有 Redis 单件；JVM/MySQL 都是多成员簇


def test_full_rebuild_keeps_candidate_status_and_sticky_id(mini_db, capsys):
    cn.main([])
    capsys.readouterr()
    jvm_id = mini_db.execute(
        "SELECT canonical_id FROM canonical_questions WHERE title_norm=?", (N_JVM,)).fetchone()[0]
    qkey = "h9:candidate"
    _add_source_and_question(mini_db, "h9", 1, "jvm内存模型", qkey)
    mini_db.execute(
        "INSERT INTO merge_candidates(q_key,canonical_id,similarity,status,created_at,updated_at) "
        "VALUES(?,?,?, 'approved', 'now', 'now')",
        (qkey, jvm_id, 0.667))
    mini_db.commit()

    cn.main([])                                  # 全量重建
    capsys.readouterr()
    assert mini_db.execute("SELECT COUNT(*) FROM merge_candidates").fetchone()[0] == 1
    assert mini_db.execute("SELECT status FROM merge_candidates WHERE q_key=?",
                           (qkey,)).fetchone()[0] == "approved"
    assert mini_db.execute(
        "SELECT canonical_id FROM canonical_questions WHERE title_norm=?", (N_JVM,)).fetchone()[0] == jvm_id


def test_sticky_id_wins_over_new_representative(mini_db):
    """同一批成员换了代表 norm，旧 canonical_id 也不能漂移。"""
    qs = [
        {"doc_hash": "h1", "seq": 1, "q_norm": "什么是jvm内存模型", "q_key": "h1:a",
         "a_text": "", "topic": "JVM"},
        {"doc_hash": "h2", "seq": 1, "q_norm": "jvm内存模型详解", "q_key": "h2:b",
         "a_text": "", "topic": "JVM"},
    ]
    clusters = [{"members": [0, 1]}]
    rows_c, _rows_v, aliases = cn.build_rows(
        qs, clusters, {"h1": ("JVM", "c1"), "h2": ("JVM", "c2")}, "now",
        sticky={"h1:a": "oldid", "h2:b": "oldid"})
    assert rows_c[0][0] == "oldid"
    assert aliases == {}


def test_incremental_candidate_rollback_removes_batch_candidate(mini_db, capsys, tmp_path):
    """候选属于本批产生的新状态；回滚必须删掉，不能留 pending 污染候选池。"""
    from mianjing_kb import manifests
    cn.main([])
    capsys.readouterr()
    qkey = "h9:candidate"
    _add_source_and_question(mini_db, "h9", 1, "jvm内存模型", qkey)
    assert cn.main(["--incremental", "--batch-id", "t-cand"]) == 0
    capsys.readouterr()
    mf = manifests.load("t-cand", tmp_path / "build")
    assert mf and mf["status"] == "committed" and len(mf["candidates"]) == 1
    assert mini_db.execute("SELECT status FROM merge_candidates WHERE q_key=?",
                           (qkey,)).fetchone()[0] == "pending"

    out = manifests.rollback(mini_db, "t-cand", apply=True, build_dir=tmp_path / "build")
    assert out["removed_candidates"] == 1
    assert mini_db.execute("SELECT COUNT(*) FROM merge_candidates WHERE q_key=?",
                           (qkey,)).fetchone()[0] == 0


def test_manifest_two_phase_prepare_then_commit(tmp_path):
    from mianjing_kb import manifests
    mf = manifests.prepare("t-phase", {"added_variants": []}, tmp_path / "build")
    assert mf["status"] == "prepared"
    assert manifests.load("t-phase", tmp_path / "build")["status"] == "prepared"
    manifests.mark_committed("t-phase", tmp_path / "build")
    assert manifests.load("t-phase", tmp_path / "build")["status"] == "committed"


def test_incremental_candidate_keeps_approved_and_apply_consumes(mini_db, capsys, tmp_path):
    cn.main([])
    capsys.readouterr()
    jvm_id = mini_db.execute(
        "SELECT canonical_id FROM canonical_questions WHERE title_norm=?", (N_JVM,)).fetchone()[0]
    qkey = "h9:candidate"
    _add_source_and_question(mini_db, "h9", 1, "jvm内存模型", qkey)
    mini_db.execute(
        "INSERT INTO merge_candidates(q_key,canonical_id,similarity,status,created_at,updated_at) "
        "VALUES(?,?,?, 'approved', 'now', 'now')",
        (qkey, jvm_id, 0.667))
    mini_db.commit()

    assert cn.main(["--incremental", "--batch-id", "t-cand"]) == 0
    capsys.readouterr()
    # 0.667 落在候选区间：不能把 approved 重置成 pending
    assert mini_db.execute("SELECT status FROM merge_candidates WHERE q_key=?",
                           (qkey,)).fetchone()[0] == "approved"
    assert cn.main(["--apply-candidates"]) == 0
    capsys.readouterr()
    assert mini_db.execute("SELECT status FROM merge_candidates WHERE q_key=?",
                           (qkey,)).fetchone()[0] == "merged"
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants WHERE q_key=?",
                           (qkey,)).fetchone()[0] == 1


def test_incremental_replaces_variant_when_qkey_changes(mini_db, capsys):
    cn.main([])
    capsys.readouterr()
    old_key = mini_db.execute(
        "SELECT q_key FROM question_variants WHERE doc_hash='h1' AND question_no='1'").fetchone()[0]
    new_key = "h1:newhash"
    mini_db.execute("UPDATE questions SET q_key=? WHERE doc_hash='h1' AND seq=1", (new_key,))
    mini_db.commit()
    assert cn.main(["--incremental", "--batch-id", "t-repl"]) == 0
    out = json.loads(capsys.readouterr().out)
    # q_key 变了就是新变体；旧 q_key 的 variant 在增量入口被清理，不再复用旧 variant_id
    assert out["attached_count"] >= 1
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants WHERE q_key=?",
                           (new_key,)).fetchone()[0] == 1
    assert mini_db.execute("SELECT variant_id FROM question_variants WHERE q_key=?",
                           (new_key,)).fetchone()[0] == cn.variant_id(new_key)
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants WHERE q_key=?",
                           (old_key,)).fetchone()[0] == 0
    assert mini_db.execute("SELECT COUNT(*) FROM canonical_dependents WHERE q_key=?",
                           (old_key,)).fetchone()[0] == 0
    # 下一轮不应再重复处理
    assert cn.main(["--incremental", "--batch-id", "t-repl-2"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["replaced_variants_count"] == 0


def test_stats_missing_db_does_not_create(tmp_path):
    missing = tmp_path / "missing.sqlite"
    assert cn.main(["--stats", "--db", str(missing)]) == 2
    assert not missing.exists()


def test_incremental_dry_run_computes_on_memory_copy(mini_db, capsys):
    cn.main([])
    capsys.readouterr()
    qkey = "h6:dry"
    _add_source_and_question(mini_db, "h6", 1, N_JVM, qkey)
    before = mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0]

    assert cn.main(["--incremental", "--dry-run", "--batch-id", "t-dry"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dry_run"] is True
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants").fetchone()[0] == before
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants WHERE q_key=?",
                           (qkey,)).fetchone()[0] == 0

    assert cn.main(["--incremental", "--batch-id", "t-real"]) == 0
    assert mini_db.execute("SELECT COUNT(*) FROM question_variants WHERE q_key=?",
                           (qkey,)).fetchone()[0] == 1


def test_incremental_counts_later_member_as_attached(mini_db, capsys):
    """同一批新建簇的后续成员应计 attached，不能因 cid 已 created 就被漏掉。"""
    cn.main([])
    capsys.readouterr()
    _add_source_and_question(mini_db, "h6", 1, "全新题目甲乙丙", "h6:new", collection="c6")
    _add_source_and_question(mini_db, "h7", 1, "全新题目甲乙丙", "h7:new", collection="c7")
    assert cn.main(["--incremental", "--batch-id", "t-count"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["created_canonicals_count"] == 1
    assert out["created_questions"] == 1
    assert out["attached_count"] == 1


def test_incremental_does_not_store_unclassified_topic(mini_db, capsys):
    """增量新簇也要走多数票/过滤口径，不能把“未分类”原样写成 canonical topic。"""
    cn.main([])
    capsys.readouterr()
    _add_source_and_question(mini_db, "h6", 1, "完全新的未分类题", "h6:unc",
                            topic=config.UNCLASSIFIED, collection="c6")
    assert cn.main(["--incremental", "--batch-id", "t-unc"]) == 0
    capsys.readouterr()
    row = mini_db.execute(
        "SELECT topic FROM canonical_questions WHERE title_norm='完全新的未分类题'").fetchone()
    assert row is not None and row["topic"] is None


def test_paraphrase_variants_merge_across_docs(mini_db, capsys):
    """正向能力：同一道题的不同写法（不是完全同文），jaccard≥阈值时必须跨文档合成一簇。

    这是小规模测试"0 合并"没覆盖到的一半：0 合并只能证明"不误合"。
    """
    long_form = "mysql索引为什么用b+树不用b树"
    short_form = "mysql索引为什么用b+树"
    assert cn.similarity(long_form, short_form) >= cn.ATTACH_JAC
    _add_source_and_question(mini_db, "h8", 1, long_form, "h8:idx", topic="MySQL", collection="c8")
    _add_source_and_question(mini_db, "h9", 1, short_form, "h9:idx", topic="MySQL", collection="c9")
    assert cn.main([]) == 0

    cids = {r["q_key"]: r["canonical_id"] for r in mini_db.execute(
        "SELECT q_key, canonical_id FROM question_variants").fetchall()}
    assert cids["h8:idx"] == cids["h9:idx"]
    members = mini_db.execute("SELECT COUNT(*) FROM canonical_dependents WHERE canonical_id=?",
                              (cids["h8:idx"],)).fetchone()[0]
    assert members == 2
    cluster = mini_db.execute("SELECT witness FROM canonical_questions WHERE canonical_id=?",
                              (cids["h8:idx"],)).fetchone()
    assert cluster["witness"] == 2                      # c8 + c9 两个来源


def test_full_rebuild_splits_transitive_tail_and_uses_star_anchor(mini_db):
    texts = ["mysql索引为什么用b+树不用b树", "mysql索引为什么用b+树",
             "为什么mysql索引用b+树", "为什么mysql索引使用b+树"]
    for i, text in enumerate(texts):
        _add_source_and_question(mini_db, f"hs{i}", 1, text, f"hs{i}:star",
                                 topic="MySQL", collection=f"cs{i}")
    assert cn.main([]) == 0

    cids = {r["q_key"]: r["canonical_id"] for r in mini_db.execute(
        "SELECT q_key, canonical_id FROM question_variants WHERE q_key LIKE 'hs%:star'")}
    assert cids["hs0:star"] == cids["hs1:star"] == cids["hs2:star"]
    assert cids["hs3:star"] != cids["hs0:star"]
    title = mini_db.execute(
        "SELECT title_norm FROM canonical_questions WHERE canonical_id=?",
        (cids["hs0:star"],)).fetchone()[0]
    assert title == texts[1]


def test_incremental_keeps_existing_anchor_without_transitive_attach(mini_db):
    cn.main([])
    texts = ["mysql索引为什么用b+树不用b树", "mysql索引为什么用b+树",
             "为什么mysql索引用b+树", "为什么mysql索引使用b+树"]
    for i, text in enumerate(texts):
        _add_source_and_question(mini_db, f"hi{i}", 1, text, f"hi{i}:star",
                                 topic="MySQL", collection=f"ci{i}")
    assert cn.main(["--incremental", "--batch-id", "t-star"]) == 0

    cids = {r["q_key"]: r["canonical_id"] for r in mini_db.execute(
        "SELECT q_key, canonical_id FROM question_variants WHERE q_key LIKE 'hi%:star'")}
    assert cids["hi0:star"] == cids["hi1:star"]
    assert cids["hi2:star"] == cids["hi3:star"]
    assert cids["hi0:star"] != cids["hi2:star"]
    norms = {f"hi{i}:star": text for i, text in enumerate(texts)}
    for first, second in (("hi0:star", "hi1:star"), ("hi2:star", "hi3:star")):
        title = mini_db.execute(
            "SELECT title_norm FROM canonical_questions WHERE canonical_id=?",
            (cids[first],)).fetchone()[0]
        assert cn.similarity(title, norms[second]) >= cn.ATTACH_JAC


def test_near_miss_band_variants_stay_split(mini_db, capsys):
    """口径边界（不是 bug）：字面差异大到 jaccard 落在 0.65–0.7 的改写会被拆开。

    `什么是springmvc` / `springmvc是什么` 是同题换语序，相似度 0.692 < 0.7：
    全量重建按契约不合并，也不登记人工候选（merge_candidates=0）——这是当前能力上限。
    """
    a, b = "什么是springmvc", "springmvc是什么"
    assert cn.similarity(a, b) < cn.ATTACH_JAC
    _add_source_and_question(mini_db, "h8", 1, a, "h8:mvc", topic="JVM", collection="c8")
    _add_source_and_question(mini_db, "h9", 1, b, "h9:mvc", topic="JVM", collection="c9")
    assert cn.main([]) == 0

    cids = {r["q_key"]: r["canonical_id"] for r in mini_db.execute(
        "SELECT q_key, canonical_id FROM question_variants").fetchall()}
    assert cids["h8:mvc"] != cids["h9:mvc"]
    assert mini_db.execute("SELECT COUNT(*) FROM merge_candidates").fetchone()[0] == 0



def test_rollback_recomputes_topic_from_surviving_members(mini_db, tmp_path):
    """回滚删掉的多票变体不能把 topic 留在簇上：按剩余成员重算 topic 与 witness。"""
    from mianjing_kb import manifests
    cn.main([])
    # 两道 Kafka 文档里的同题变体吸附到 Redis 单件簇：多数票把 topic 投成 Kafka
    for h, seq, col in (("h8", 1, "c8"), ("h9", 1, "c9")):
        _add_source_and_question(mini_db, h, seq, N_REDIS, f"{h}:{N_REDIS[:8]}",
                                 topic="Kafka", collection=col)
    assert cn.main(["--incremental", "--batch-id", "t2"]) == 0
    row = mini_db.execute("SELECT topic, witness FROM canonical_questions "
                          "WHERE title_norm=?", (N_REDIS,)).fetchone()
    assert row["topic"] == "Kafka" and row["witness"] == 3

    out = manifests.rollback(mini_db, "t2", apply=True, build_dir=tmp_path / "build")

    assert out["ok"] and out["status"] == "rolled_back"
    assert set(out["recomputed_topics"].values()) == {"Redis"}
    row = mini_db.execute("SELECT topic, witness FROM canonical_questions "
                          "WHERE title_norm=?", (N_REDIS,)).fetchone()
    assert row["topic"] == "Redis" and row["witness"] == 1


def test_rollback_refuses_manifest_from_another_database(mini_db, tmp_path):
    """manifests 目录全局共享：拿 A 库的批次去回滚 B 库必须拒绝。"""
    from mianjing_kb import manifests
    build = tmp_path / "build"
    manifests.prepare("mA", {"added_variants": ["h1:dead"], "touched_canonicals": []},
                      build, conn=mini_db)
    other = sqlite3.connect(tmp_path / "other.sqlite")
    other.row_factory = sqlite3.Row
    try:
        out = manifests.rollback(other, "mA", apply=True, build_dir=build)
    finally:
        other.close()
    assert out["ok"] is False and out["status"] == "db_mismatch"
    assert "另一个库" in out["why"]
    assert manifests.load("mA", build)["status"] == "prepared"   # 拒绝时不得改状态
