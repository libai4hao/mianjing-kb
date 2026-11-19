# -*- coding: utf-8 -*-
"""classify --neighbor --vec 测试：语义投票应用/拒绝/落库（embeddings 用假向量离线跑）。"""
import sqlite3

import pytest
from mianjing_kb import embed_questions  # noqa: E402
from mianjing_kb import classify as cl  # noqa: E402
from mianjing_kb.kb import config  # noqa: E402

REAL_META = config.META_DIR


def fake_embed_texts(texts):
    """确定性假向量：mysql/索引→[1,0]；java/并发/多线程→[0,1]；其余→[0.7,0.7]（两不沾）。"""
    out = []
    for t in texts:
        low = t.lower()
        if "mysql" in low or "索引" in t:
            out.append([1.0, 0.0])
        elif "java" in low or "并发" in t or "多线程" in t:
            out.append([0.0, 1.0])
        else:
            out.append([0.7, 0.7])
    return out


@pytest.fixture()
def mini_db(tmp_path, monkeypatch):
    build = tmp_path / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    monkeypatch.setattr(embed_questions, "embed_texts", fake_embed_texts)
    conn = sqlite3.connect(build / "kb.sqlite")
    conn.row_factory = sqlite3.Row
    conn.executescript((REAL_META / "schema" / "001_init.sql").read_text(encoding="utf-8"))
    rows = [
        ("h1", "Java并发编程面试题.pdf", "并发编程", "classified"),
        ("h2", "MySQL索引优化面试题.pdf", "MySQL", "classified"),
        ("h3", "Java多线程面试题.pdf", "未分类", "classified"),      # → 语义命中 并发编程
        ("h4", "计算机网络面试题.pdf", "未分类", "classified"),      # → 两不沾 → 待人工
    ]
    for i, (h, name, topic, status) in enumerate(rows):
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
            "dir_path, status, batch_id, first_seen_at, last_seen_at, topic, category) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (h, h + "0" * 48, name, ".pdf", 1, f"x/{name}", "12-综合题库", status, "t",
             "now", "now", topic, "题库"))
    conn.commit()
    yield conn
    conn.close()


def test_apply_and_review_dry_run(mini_db):
    rc = cl.run_neighbor_vec(mini_db, dry_run=True)
    assert rc == 0
    # dry-run 不落库
    assert mini_db.execute("SELECT topic FROM source_documents WHERE doc_hash='h3'").fetchone()[0] == "未分类"
    # 待人工清单包含两件（applied 也入清单，proposal 未加待人工标记）
    import csv
    rp = config.BUILD_DIR / "topic_neighbor_vec_review.csv"
    rows = list(csv.DictReader(rp.open(encoding="utf-8-sig")))
    assert {r["file_name"] for r in rows} == {"Java多线程面试题.pdf", "计算机网络面试题.pdf"}
    h4 = next(r for r in rows if r["file_name"] == "计算机网络面试题.pdf")
    assert h4["proposal_topic"].startswith("<待人工")


def test_apply_writes_db(mini_db):
    cl.run_neighbor_vec(mini_db, dry_run=False)
    row = mini_db.execute(
        "SELECT topic, sub_topic, topic_source, classifier FROM source_documents "
        "WHERE doc_hash='h3'").fetchone()
    assert row["topic"] == "并发编程" and row["topic_source"] == "neighbor"
    assert row["classifier"] == "neighbor"
    # 两不沾件不动
    assert mini_db.execute(
        "SELECT topic FROM source_documents WHERE doc_hash='h4'").fetchone()[0] == "未分类"


def test_vote_semantic_low_conf_rejects():
    # 票力均分 → conf=0.5 < 0.6 → 拒绝
    res = cl.vote_semantic([(0.9, "MySQL", "", "a.pdf"), (0.9, "Redis", "", "b.pdf")])
    assert res["topic"] == "未分类" and res["confidence"] == 0.5
    # 单一强票 → 通过
    res2 = cl.vote_semantic([(0.88, "MySQL", "索引", "a.pdf")])
    assert res2["topic"] == "MySQL" and res2["sub_topic"] == "索引"
    # 空票
    assert cl.vote_semantic([])["topic"] == "未分类"


def test_duplicate_stems_keep_vectors_by_doc_hash(mini_db, monkeypatch):
    """相同文件名 stem 不得互相覆盖；目录上下文与 doc_hash 均参与定位。"""
    mini_db.execute("UPDATE source_documents SET file_name='same.pdf', dir_path='A' WHERE doc_hash='h1'")
    mini_db.execute("UPDATE source_documents SET file_name='same.pdf', dir_path='B' WHERE doc_hash='h2'")
    # 未分类目标与 A 中的 ref 相似，不应被 B 的同 stem 向量覆盖。
    mini_db.execute("UPDATE source_documents SET file_name='target.pdf', dir_path='A' WHERE doc_hash='h3'")
    mini_db.commit()

    def contextual(texts):
        out = []
        for t in texts:
            if t.startswith("A/"):
                out.append([1.0, 0.0])
            else:
                out.append([0.0, 1.0])
        return out

    monkeypatch.setattr(embed_questions, "embed_texts", contextual)
    # target 的输入也是 A/，因此只与同目录 A 参照一致。
    rc = cl.run_neighbor_vec(mini_db, dry_run=False)
    assert rc == 0
    assert mini_db.execute("SELECT topic FROM source_documents WHERE doc_hash='h3'").fetchone()[0] == "并发编程"
