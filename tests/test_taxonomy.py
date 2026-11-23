# -*- coding: utf-8 -*-
"""标签配置表（T24）+ 手动模板/回灌 + 离线规则打标。"""
import csv
import json

import pytest
from mianjing_kb import batch_label as bl
from mianjing_kb.kb import db, taxonomy
from mianjing_kb.kb.vocab import topic_aliases, topic_vocab


@pytest.fixture()
def conn(tmp_path):
    c = db.connect(tmp_path / "kb.sqlite")
    for qid, q_text in ((1, "什么是 JVM 的类加载？"), (2, "手写 LRU 缓存"),
                        (3, "为什么 Redis 快？")):
        c.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, "
                  "q_norm, q_norm_hash, simhash, span_start, span_end, topic, extracted_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (qid, f"h{qid}", qid, q_text, "答案" * 30, "八股", "high",
                   f"norm{qid}", f"hash{qid}", 0, 0, 1, "", "now"))
    c.commit()
    yield c
    c.close()


def test_taxonomy_seeded_and_loaded(conn):
    snap = taxonomy.load(conn)
    assert snap["source"] == "table"
    assert len(snap["tags"]) >= 60
    l1 = [g["level1"] for g in snap["groups"]]
    assert "Java" in l1 and "AI与大模型" in l1
    assert snap["aliases"].get("jvm gc") == "JVM"


def test_taxonomy_add_and_disable(conn):
    assert taxonomy.add(conn, "Java", "Netty") == "added"
    assert taxonomy.add(conn, "Java", "Netty") == "exists"
    assert "Netty" in taxonomy.load(conn)["tags"]
    assert taxonomy.disable(conn, "Java/Netty") == 1
    assert "Netty" not in taxonomy.load(conn)["tags"]


def test_seed_persists_without_caller_commit(tmp_path):
    """P1-2（docs/47）：--seed 曾只 INSERT 不 commit，打印 seeded 却回滚。

    断言口径是"写完关连接、重开再读"，不是"同一连接里能看到"。
    """
    path = tmp_path / "seed.sqlite"
    expected = len(taxonomy.load()["tags"])
    c = db.connect(path)
    taxonomy.seed(c, only_if_empty=False)       # 013 迁移可能已灌过一部分，只看最终行数
    c.close()                                   # 故意不显式 commit
    c2 = db.connect(path, create=False)
    rows = c2.execute("SELECT COUNT(*) FROM label_taxonomy").fetchone()[0]
    c2.close()
    assert rows == expected


def test_every_taxonomy_group_is_a_legal_topic(conn):
    """一级组名必须能落到合法 topic（同名或经 topic_aliases 归一）。

    2026-11-23 实测：数据库 组名不在词表 → 54/500 条数据库题被判"其他"+pending，G6 拦 merge。
    """
    tax = taxonomy.load(conn)
    vocab = set(topic_vocab())
    aliases = topic_aliases()
    assert vocab, "topic_vocab 降级为空，本断言失效"
    missing = [g["level1"] for g in tax["groups"]
               if g["level1"] not in vocab and aliases.get(g["level1"]) not in vocab]
    assert not missing, f"taxonomy 一级组既不在 topic 词表也无别名: {missing}"


def test_topic_alias_normalized_not_to_other(conn):
    """组名走别名归一：`算法与数据结构` 应落 `算法`，而不是词表外的"其他"+pending。"""
    spec = bl.labelspec.validate_label(
        {"topic": "算法与数据结构", "qtype": "算法", "difficulty": 3,
         "canonical_q": "手写快排", "tags": []},
        topic_vocab(), q_text="手写快排", topic_aliases=topic_aliases())
    assert spec["topic"] == "算法"
    assert spec["review_state"] is None


def test_prompt_uses_taxonomy_groups(conn):
    tax = taxonomy.load(conn)
    prompt = bl.build_system_prompt(("JVM", "Redis", "其他"), tax)
    assert "Java（" in prompt and "数据库（" in prompt
    assert "position" in prompt and "canonical_q" in prompt


def test_prompt_does_not_truncate_groups_or_tags(conn):
    """新增一级分组后 prompt 必须全部带出，否则场景/HR 类问题只能落"其他"。"""
    tax = taxonomy.load(conn)
    lines = taxonomy.prompt_lines(tax)
    for g in tax["groups"]:
        assert f"{g['level1']}（" in lines
        assert g["level2"][-1] in lines, g["level1"]  # 每组最后一项也没被预算砍掉


def test_manual_template_and_import(conn, tmp_path):
    path = tmp_path / "manual.csv"
    out = bl.manual_template(conn, 2, path, seed=1)
    assert out["rows"] == 2 and path.exists()
    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")                    # UTF-8 BOM，Excel 可直接打开

    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    rows[0].update({"topic": "JVM", "qtype": "八股", "difficulty": "3",
                    "position": "后端", "quality": "", "canonical_q": "",
                    "tags": "JVM GC|类加载"})
    rows[1].update({"qid": "999", "topic": "JVM", "qtype": "八股", "difficulty": "3"})
    import csv as _csv
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=bl.MANUAL_COLUMNS)
        w.writeheader()
        w.writerows(rows)

    res = bl.manual_import(conn, path)
    assert res["imported"] == 1 and res["rejected"] == 1
    assert "qid=999" in res["errors"][0]
    staged = conn.execute("SELECT topic, position, tags, tag_norm_hits, provider, review_state "
                          "FROM labels_staging WHERE qid=?", (int(rows[0]["qid"]),)).fetchone()
    assert staged["topic"] == "JVM" and staged["provider"] == "manual"
    assert json.loads(staged["position"]) == ["后端"]
    assert json.loads(staged["tags"]) == ["JVM", "类加载"]     # JVM GC → JVM
    assert staged["tag_norm_hits"] == 1 and staged["review_state"] is None


def test_manual_import_bad_qtype_rejected(conn, tmp_path):
    path = tmp_path / "bad.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=bl.MANUAL_COLUMNS)
        w.writeheader()
        w.writerow({"qid": 1, "topic": "JVM", "qtype": "玄学", "difficulty": 3})
    res = bl.manual_import(conn, path, dry_run=True)
    assert res["imported"] == 0 and "第2行" in res["errors"][0]


def test_offline_rule_label(conn):
    q = dict(conn.execute("SELECT qid, q_norm_hash, q_text, a_text FROM questions WHERE qid=2")
             .fetchone())
    row = bl.label_one_offline(q, taxonomy.load(conn))
    assert row["provider"] == "offline" and row["review_state"] == "pending"
    assert row["qtype"] == "算法" and row["difficulty"] == 3
    assert row["canonical_q"] == "手写 LRU 缓存"
    assert json.loads(row["tags"])                                 # 至少命中一个二级标签
