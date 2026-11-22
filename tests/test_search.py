# -*- coding: utf-8 -*-
"""P3-① search 测试：RRF 融合 + kw 双路由 + hybrid 降级。"""
import sqlite3

import pytest

from mianjing_kb import search as S
from mianjing_kb.search import has_vec_table, hybrid_search, keyword_search, rag_contexts, rrf_fuse

MIRROR_REL = "题库/综合/手写题.md"
MIRROR_HASH = __import__("hashlib").sha256(("md-mirror:" + MIRROR_REL).encode()).hexdigest()[:16]


@pytest.fixture()
def mini_db(tmp_path):
    p = tmp_path / "mini.db"
    con = sqlite3.connect(str(p))
    con.row_factory = sqlite3.Row
    con.executescript(f"""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, mirror_hash TEXT, content TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
                       qtype TEXT, topic TEXT, category TEXT, difficulty INTEGER,
                       tags_json TEXT, position TEXT, quality TEXT, q_key TEXT);
CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT, PRIMARY KEY(qid, tag_norm));
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('h1', 'MySQL面试题.pdf');
INSERT INTO doc_contents(md_rel, doc_hash, mirror_hash, content) VALUES('{MIRROR_REL}', NULL, '{MIRROR_HASH}', 'x');
INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, category, difficulty, tags_json, position, quality, q_key) VALUES
(1, 'h1', 'InnoDB索引为什么用B+树不用B树？', '叶子节点链表利于范围查询。', '八股', 'MySQL', '题库', 3, '["B+树","索引"]', '["后端"]', NULL, 'h1:q1'),
(2, 'h1', 'MySQL的主从复制原理是什么？', 'binlog 传输到从库回放。', '八股', 'MySQL', '题库', 4, '["复制"]', '["后端","数据"]', '["重复"]', 'h1:q2'),
(3, 'h1', 'Redis持久化有哪几种方式？', 'RDB 与 AOF。', '八股', 'Redis', '题库', 2, '["持久化"]', '["后端"]', NULL, 'h1:q3'),
(4, '{MIRROR_HASH}', '手写LRU缓存如何实现？', '哈希表加双向链表。', '算法', '数据结构', '镜像', 5, '["LRU"]', '["算法"]', NULL, 'm:q4');
INSERT INTO question_tags(qid, tag, tag_norm) VALUES
(1,'B+树','b+树'),(1,'索引','索引'),(2,'复制','复制'),(3,'持久化','持久化'),(4,'LRU','lru');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(1, 'InnoDB索引为什么用B+树不用B树？', '叶子节点链表利于范围查询。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(2, 'MySQL的主从复制原理是什么？', 'binlog 传输到从库回放。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(3, 'Redis持久化有哪几种方式？', 'RDB 与 AOF。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(4, '手写LRU缓存如何实现？', '哈希表加双向链表。');
""")
    yield con
    con.close()


def test_rrf_fuse():
    fused = rrf_fuse([1, 2, 3], [3, 1])
    assert fused[0][0] == 1            # 两路都靠前 → 融合第一
    assert fused[0][1] > fused[1][1]
    assert len(fused) == 3
    assert rrf_fuse([], [5])[0] == (5, 1 / 61)


def test_kw_search_routes(mini_db):
    hits = keyword_search(mini_db, "主从复制", 10)          # ≥3 字走 FTS
    assert len(hits) == 1 and hits[0]["qid"] == 2
    hits = keyword_search(mini_db, "B树", 10)               # 2 字走 LIKE
    assert len(hits) == 1 and hits[0]["qid"] == 1
    hits = keyword_search(mini_db, "主从", 10, topic="MySQL")
    assert len(hits) == 1 and hits[0]["topic"] == "MySQL"


def test_hybrid_degrades_to_kw(mini_db):
    assert has_vec_table(mini_db) is False
    hits = hybrid_search(mini_db, "主从复制", 5)
    assert len(hits) == 1 and "rrf" not in hits[0]


def test_source_resolution(mini_db):
    """P3-③：documents 命中给 file_name；md 镜像题回链 doc_contents.md_rel。"""
    hits = keyword_search(mini_db, "主从复制", 5)
    assert hits[0]["source"] == "MySQL面试题.pdf"
    hits = keyword_search(mini_db, "手写LRU", 5)
    assert len(hits) == 1 and hits[0]["source"] == MIRROR_REL


def test_source_path_resolves_by_doc_hash(mini_db):
    """F2：正常文档（questions.doc_hash=源 doc_hash）也要能回链到 md，而不是恒空。"""
    mini_db.execute("INSERT INTO doc_contents(md_rel, doc_hash, mirror_hash, content) "
                    "VALUES('题库/MySQL/MySQL面试题.md','h1','r'*16,'x')")
    mini_db.commit()
    hits = keyword_search(mini_db, "主从复制", 5)
    assert hits[0]["source_path"] == "题库/MySQL/MySQL面试题.md"
    hits = keyword_search(mini_db, "手写LRU", 5)          # 孤儿 md：doc_hash 为空，走 mirror 兜底
    assert hits[0]["source_path"] == MIRROR_REL


def test_duplicate_md_rows_do_not_multiply_hits(mini_db):
    """同一 doc_hash 有多份 md（生产 48 组）时只挑一条，且优先题库/ 路径、行数不翻倍。"""
    mini_db.execute("INSERT INTO doc_contents(md_rel, doc_hash, mirror_hash, content) VALUES"
                    "('面经/MySQL/MySQL面试题.md','h1','a'*16,'x'),"
                    "('题库/MySQL/MySQL面试题.md','h1','b'*16,'x')")
    mini_db.commit()
    hits = keyword_search(mini_db, "主从复制", 5)
    assert len(hits) == 1
    assert hits[0]["source_path"] == "题库/MySQL/MySQL面试题.md"


def test_rag_contexts(mini_db):
    out = rag_contexts(mini_db, "手写LRU", 3)
    ctx = out["contexts"]
    assert out["query"] == "手写LRU"
    assert len(ctx) == 1
    assert ctx[0]["answer"].startswith("哈希表加双向链表")
    assert ctx[0]["source"] == MIRROR_REL


def test_keyword_filters_difficulty_position_quality_tags(mini_db):
    hits = keyword_search(mini_db, "主从复制", 10, difficulty=4, position="后端",
                          quality="重复", tags=["复制"])
    assert len(hits) == 1 and hits[0]["qid"] == 2
    assert hits[0]["difficulty"] == 4 and hits[0]["tags_json"] == '["复制"]'


def test_keyword_tags_are_and(mini_db):
    assert keyword_search(mini_db, "InnoDB", 10, tags=["B+树", "索引"])[0]["qid"] == 1
    assert keyword_search(mini_db, "InnoDB", 10, tags=["B+树", "复制"]) == []


def test_vector_old_schema_filter_oversamples(tmp_path, monkeypatch):
    """旧向量表没有 metadata：topic 只能后过滤，必须过采样，不能只取 limit 条后过滤成 0。"""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    from mianjing_kb import embed_questions as E

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(
        "CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT, "
        "qtype TEXT, topic TEXT, difficulty INTEGER, tags_json TEXT, position TEXT, quality TEXT, "
        "q_key TEXT);"
        "CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);"
        "CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, "
        "mirror_hash TEXT, content TEXT);"
        "CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);"
        f"CREATE VIRTUAL TABLE vec_questions USING vec0(embedding float[{E.DIM}]);")

    def vec(first: float, second: float) -> list[float]:
        vals = [0.0] * E.DIM
        vals[0], vals[1] = first, second
        return vals

    for i in range(1, 21):
        topic = "B" if i <= 10 else "A"
        con.execute("INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, difficulty, q_key) "
                    "VALUES (?, 'h', ?, '', '八股', ?, 3, ?)", (i, f"题{i}", topic, f"h:{i}"))
        near = 0.01 if topic == "B" else 0.5 + i / 1000
        con.execute("INSERT INTO vec_questions(rowid, embedding) VALUES (?,?)",
                    (i, sqlite_vec.serialize_float32(vec(1.0, near))))
    con.commit()
    monkeypatch.setattr(E, "embed_texts", lambda texts: [vec(1.0, 0.0) for _ in texts])
    rows = S.vector_search(con, "任意", 5, topic="A")
    assert len(rows) == 5
    assert {r["topic"] for r in rows} == {"A"}
    con.close()


def test_hybrid_model_failure_degrades_to_kw(mini_db, monkeypatch):
    """向量表在但模型不可用：hybrid 必须降级关键词，而不是整体失败。"""
    from mianjing_kb import embed_questions as E

    def boom(texts):
        raise RuntimeError("model not ready")

    monkeypatch.setattr(E, "embed_texts", boom)
    diag = {}
    # 伪造 vec_questions 存在；当前 mini_db 没有 vec 扩展，has_vec_table 只查 sqlite_master
    mini_db.execute("CREATE TABLE vec_questions(x)")
    hits = hybrid_search(mini_db, "主从复制", 5, diagnostics=diag)
    assert len(hits) == 1 and hits[0]["qid"] == 2
    assert diag["vector"] == "unavailable" and "model not ready" in diag["why"]


def test_vector_filter_pushed_into_knn(tmp_path, monkeypatch):
    """T1 核心：B 类向量更近，topic=A 过滤必须在 KNN 内完成，否则后过滤会 0 条。"""
    sqlite_vec = pytest.importorskip("sqlite_vec")
    from mianjing_kb import embed_questions as E

    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.enable_load_extension(True)
    sqlite_vec.load(con)
    con.enable_load_extension(False)
    con.executescript(
        "CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT, "
        "qtype TEXT, topic TEXT, difficulty INTEGER, tags_json TEXT, position TEXT, quality TEXT, "
        "q_key TEXT);"
        "CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);"
        "CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, "
        "mirror_hash TEXT, content TEXT);"
        "CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);")
    E.create_vec_table(con)

    def vec(first: float, second: float) -> list[float]:
        vals = [0.0] * E.DIM
        vals[0], vals[1] = first, second
        return vals

    # 10 条 B 更靠近查询，10 条 A 更远：后过滤会拿不到 A，metadata 下推才正确。
    for i in range(1, 21):
        topic = "B" if i <= 10 else "A"
        qid = i
        con.execute("INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, difficulty, q_key) "
                    "VALUES (?, 'h', ?, '', '八股', ?, 3, ?)", (qid, f"题{qid}", topic, f"h:{qid}"))
        near = 0.01 if topic == "B" else 0.5 + i / 1000
        con.execute("INSERT INTO vec_questions(rowid, embedding, topic, qtype, difficulty, q_key) "
                    "VALUES (?,?,?,?,?,?)",
                    (qid, sqlite_vec.serialize_float32(vec(1.0, near)), topic, "八股", 3, f"h:{qid}"))
    con.commit()

    monkeypatch.setattr(E, "embed_texts", lambda texts: [vec(1.0, 0.0) for _ in texts])
    rows = S.vector_search(con, "任意", 5, topic="A")
    assert len(rows) == 5
    assert {r["topic"] for r in rows} == {"A"}
    con.close()


def _dedupe_db(path):
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript("""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, mirror_hash TEXT, content TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
                       qtype TEXT, topic TEXT, category TEXT, difficulty INTEGER,
                       tags_json TEXT, position TEXT, quality TEXT, q_key TEXT, q_norm_hash TEXT);
CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
""")
    return con


def _insert_dedupe_row(con, qid, doc, text, answer, qkey, norm, topic="Java基础"):
    con.execute("INSERT INTO documents VALUES(?,?)", (doc, f"{doc}.pdf"))
    con.execute("INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, difficulty, q_key, q_norm_hash) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (qid, doc, text, answer, "八股", topic, 3, qkey, norm))
    con.execute("INSERT INTO questions_fts(rowid, q_text, a_text) VALUES (?,?,?)", (qid, text, answer))


def test_rag_exact_dedupe_by_qnorm_hash(tmp_path):
    con = _dedupe_db(tmp_path / "dedupe.db")
    _insert_dedupe_row(con, 1, "d1", "HashMap 的工作原理是什么", "数组加链表。", "d1:q1", "norm1")
    _insert_dedupe_row(con, 2, "d2", "HashMap 的工作原理是什么？", "数组加链表。", "d2:q2", "norm1")
    con.commit()
    out = rag_contexts(con, "HashMap", 5)
    assert len(out["contexts"]) == 1
    assert out["contexts"][0]["sources"] == ["d1.pdf", "d2.pdf"]
    assert out["contexts"][0]["duplicates"] == 2
    con.close()


def test_rag_exact_dedupe_does_not_merge_different_topics(tmp_path):
    con = _dedupe_db(tmp_path / "topic.db")
    _insert_dedupe_row(con, 1, "d1", "Java 集合题", "答案", "d1:q1", "same", topic="Java基础")
    _insert_dedupe_row(con, 2, "d2", "Java 集合题", "答案", "d2:q2", "same", topic="MySQL")
    con.commit()
    out = rag_contexts(con, "Java 集合", 5)
    assert len(out["contexts"]) == 2
    con.close()


def test_rag_cluster_dedupe_keeps_one_representative(tmp_path):
    con = _dedupe_db(tmp_path / "cluster.db")
    con.executescript("CREATE TABLE canonical_dependents(q_key TEXT PRIMARY KEY, canonical_id TEXT);")
    _insert_dedupe_row(con, 1, "d1", "Java 集合 List 和 Set 区别", "List 有序。", "d1:q1", "n1")
    _insert_dedupe_row(con, 2, "d2", "Java 集合 List 与 Set 有什么不同", "List 有序。", "d2:q2", "n2")
    con.executemany("INSERT INTO canonical_dependents VALUES(?,?)",
                    [("d1:q1", "cluster-1"), ("d2:q2", "cluster-1")])
    con.commit()
    out = rag_contexts(con, "Java 集合", 5)
    assert len(out["contexts"]) == 1
    assert out["contexts"][0]["duplicates"] == 2
    con.close()


def test_rag_cluster_keeps_second_variant_when_answer_differs(tmp_path):
    con = _dedupe_db(tmp_path / "variant.db")
    con.executescript("CREATE TABLE canonical_dependents(q_key TEXT PRIMARY KEY, canonical_id TEXT);")
    _insert_dedupe_row(con, 1, "d1", "Java 集合 List 和 Set 区别", "List 有序，Set 无序。", "d1:q1", "n1")
    _insert_dedupe_row(con, 2, "d2", "Java 集合 List 与 Set 有什么不同", "底层实现完全不同，一个数组一个哈希。", "d2:q2", "n2")
    con.executemany("INSERT INTO canonical_dependents VALUES(?,?)",
                    [("d1:q1", "cluster-1"), ("d2:q2", "cluster-1")])
    con.commit()
    out = rag_contexts(con, "Java 集合", 5)
    assert len(out["contexts"]) == 2          # 答案明显不同 → 最多保留两个变体
    con.close()


def test_rag_context_budget_keeps_rank_order(tmp_path):
    con = _dedupe_db(tmp_path / "budget.db")
    for i in range(1, 4):
        _insert_dedupe_row(con, i, f"d{i}", f"Java 集合题{i}", "长答案" * 400, f"d{i}:q{i}", f"n{i}")
    con.commit()
    out = rag_contexts(con, "Java 集合", 3, max_context_chars=500)
    ctx = out["contexts"]
    assert out["context_chars"] <= 500
    assert len(ctx) == 3
    assert len(ctx[0]["answer"]) > len(ctx[1]["answer"])   # 预算优先给排序靠前的
    assert ctx[0]["qid"] == 1
    con.close()


def test_rag_context_budget_drops_only_unaffordable(tmp_path):
    con = _dedupe_db(tmp_path / "drop.db")
    _insert_dedupe_row(con, 1, "d1", "Java 集合长题", "长答案" * 400, "d1:q1", "n1")
    _insert_dedupe_row(con, 2, "d2", "Java 集合短题", "短答案", "d2:q2", "n2")
    con.commit()
    out = rag_contexts(con, "Java 集合", 2, max_context_chars=300)
    by_id = {c["qid"]: c for c in out["contexts"]}
    assert set(by_id) == {1, 2}            # 短答案不会被预算误丢
    assert len(by_id[1]["answer"]) < 800
    assert out["context_chars"] <= 300
    con.close()


def test_rag_context_rejects_negative_budget(tmp_path):
    con = _dedupe_db(tmp_path / "negative.db")
    _insert_dedupe_row(con, 1, "d1", "Java 集合", "答案", "d1:q1", "n1")
    con.commit()
    with pytest.raises(ValueError):
        rag_contexts(con, "Java 集合", 1, max_context_chars=-1)
    con.close()


@pytest.fixture()
def entity_db(tmp_path):
    """两个英文实体各一条：整句“X 是什么”在原文里不存在，只能靠实体词命中。"""
    con = sqlite3.connect(str(tmp_path / "entity.db"))
    con.row_factory = sqlite3.Row
    con.executescript("""
CREATE TABLE documents(doc_hash TEXT PRIMARY KEY, file_name TEXT);
CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, doc_hash TEXT, mirror_hash TEXT, content TEXT);
CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, q_text TEXT, a_text TEXT,
                       qtype TEXT, topic TEXT, category TEXT, difficulty INTEGER,
                       tags_json TEXT, position TEXT, quality TEXT, q_key TEXT);
CREATE TABLE question_tags(qid INTEGER, tag TEXT, tag_norm TEXT);
CREATE VIRTUAL TABLE questions_fts USING fts5(q_text, a_text, tokenize='trigram');
INSERT INTO documents VALUES('h1', 'Java面试题.pdf');
INSERT INTO questions(qid, doc_hash, q_text, a_text, qtype, topic, category, difficulty, tags_json, position, quality, q_key) VALUES
(1, 'h1', 'Kafka 是什么？', '分布式消息队列。', '八股', 'Kafka', '题库', 2, NULL, NULL, NULL, 'h1:q1'),
(2, 'h1', '什么是 ThreadLocal?', '线程本地变量。', '八股', '并发编程', '题库', 3, NULL, NULL, NULL, 'h1:q2');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(1, 'Kafka 是什么？', '分布式消息队列。');
INSERT INTO questions_fts(rowid, q_text, a_text) VALUES(2, '什么是 ThreadLocal?', '线程本地变量。');
""")
    yield con
    con.close()


def test_latin_terms_extracts_entities():
    """F1：查询里的拉丁实体词单独抽出来（≥3 字符，按出现顺序去重）。"""
    assert S.latin_terms("ThreadLocal 是什么") == ["ThreadLocal"]
    assert S.latin_terms("HashMap 和 ConcurrentHashMap 有什么区别") == [
        "HashMap", "ConcurrentHashMap"]
    assert S.latin_terms("Redis 持久化 redis 方式") == ["Redis"]      # 大小写去重
    assert S.latin_terms("B树与红黑树") == []                          # 单字母/中文不抽


def test_entity_route_terms_only_single_entity_short_query():
    """F1 闸门：只对"单实体 + 少量中文"启用词项路由/实体聚焦。"""
    assert S.entity_route_terms("ThreadLocal 是什么") == ["ThreadLocal"]
    assert S.entity_route_terms("Redis 持久化") == ["Redis"]
    assert S.entity_route_terms("JVM 内存区域划分") == ["JVM"]
    assert S.entity_route_terms("Spring Boot 好在哪里") == []            # 双实体
    assert S.entity_route_terms("GET POST 区别和 HTTP 状态码") == []       # 三实体
    assert S.entity_route_terms("什么是 Spring 框架，它有哪些主要模块") == []  # 长中文


def test_keyword_entity_phrase_falls_back_to_term_route(entity_db):
    """F1 核心：整句“ThreadLocal 是什么”匹配不到原文，必须回退到实体词命中。"""
    hits = keyword_search(entity_db, "ThreadLocal 是什么", 10)
    assert [h["qid"] for h in hits] == [2]
    hits = keyword_search(entity_db, "Kafka 是什么", 10)
    assert [h["qid"] for h in hits] == [1]


def test_keyword_entity_term_route_handles_space_variant(mini_db):
    """'Redis 持久化'（带空格）与原文 'Redis持久化…' 不同串，整句 LIKE 也救不了。"""
    assert keyword_search(mini_db, "Redis 持久化", 10)[0]["qid"] == 3


def test_keyword_entity_route_does_not_flood_long_queries(entity_db):
    """长问句不启用词项路由：否则 'Spring' 这类泛实体会用 30 条噪声行淹掉向量结果。"""
    assert keyword_search(entity_db, "什么是 Spring 框架，它有哪些主要模块", 10) == []


def test_entity_tiebreak_only_breaks_ties():
    """F1 并列破除：同分时命中实体词的题优先；分差存在时顺序不动。"""
    by_qid = {1: {"qid": 1, "q_text": "Kafka 是什么？", "a_text": ""},
              2: {"qid": 2, "q_text": "什么是 ThreadLocal?", "a_text": ""}}
    assert [q for q, _ in S.entity_tiebreak([(1, 0.01), (2, 0.01)], by_qid,
                                            ["ThreadLocal"])] == [2, 1]
    assert [q for q, _ in S.entity_tiebreak([(1, 0.02), (2, 0.01)], by_qid,
                                            ["ThreadLocal"])] == [1, 2]
    assert S.entity_tiebreak([(1, 0.01)], by_qid, []) == [(1, 0.01)]

