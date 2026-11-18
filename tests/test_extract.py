# -*- coding: utf-8 -*-
"""extract（P1-1 题块落表）回归测试：临时语料 + 临时库，端到端。"""
import json
from pathlib import Path

import pytest

from tools import extract
from tools.kb import config


@pytest.fixture()
def tmp_env(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    build.mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    monkeypatch.setattr(config, "QUESTION_MD_DIR", corpus / "20-题库MD")
    (corpus / "20-题库MD").mkdir()
    from tools.kb import db as kbdb
    conn = kbdb.connect(build / "kb.sqlite")   # 完整迁移（001-007，与生产 connect 同路径）
    yield corpus, conn
    conn.close()


def _seed_doc(conn, corpus: Path, name: str, text: str):
    import hashlib
    raw = text.encode("utf-8")
    p = corpus / name
    p.write_text(text, encoding="utf-8")
    h = hashlib.sha256(raw).hexdigest()[:16]
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (h, h, name, ".md", len(raw), str(p), "", "classified", "t", "now", "now"))
    conn.commit()
    return h, p


MD_A = """# 面试整理

1. 什么是 Redis 持久化？

RDB 快照与 AOF 日志两种方式。

2. Redis 为什么这么快？

纯内存操作加 IO 多路复用。
"""

MD_B = """# 另一份资料

1、请简单介绍一下自己？

我有三年后端经验，主要负责订单与支付系统的服务端开发，熟悉高并发场景下的缓存与消息队列落地实践。

2、什么是 Redis 持久化？

同前一份，RDB 与 AOF。RDB 按策略定期落盘快照，AOF 追加写命令日志，重启时回放恢复数据。
"""


def test_extract_end_to_end(tmp_env):
    corpus, conn = tmp_env
    h1, _ = _seed_doc(conn, corpus, "a.md", MD_A)
    h2, _ = _seed_doc(conn, corpus, "b.md", MD_B)
    rows1 = extract.extract_doc(h1, corpus / "a.md")
    rows2 = extract.extract_doc(h2, corpus / "b.md")
    # a.md：2 题号题；b.md：2 题号题
    assert [r["seq"] for r in rows1] == [1, 2]
    assert len(rows2) == 2
    assert all(r["conf"] == "high" for r in rows1)
    assert rows1[0]["qtype"] in ("八股", "场景")
    # 落库 + 频次视图：两份文档都有"什么是 Redis 持久化"→ n_distinct_docs=2
    extract.store(conn, h1, rows1)
    extract.store(conn, h2, rows2)
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    assert n == 4
    freq = conn.execute(
        "SELECT sample_q, n_distinct_docs FROM question_freq ORDER BY n_distinct_docs DESC").fetchall()
    assert len(freq) >= 1 and freq[0]["n_distinct_docs"] == 2
    assert "持久化" in freq[0]["sample_q"]
    # 幂等：重跑同 doc 不翻倍
    extract.store(conn, h1, rows1)
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 4


def test_store_reattaches_label_facts(tmp_env):
    """B1 修复：重切（删了重插，qid 变）后按 q_norm_hash 重挂 label_facts 里的打标。"""
    corpus, conn = tmp_env
    h1, _ = _seed_doc(conn, corpus, "a.md", MD_A)
    rows1 = extract.extract_doc(h1, corpus / "a.md")
    n = extract.store(conn, h1, rows1)
    conn.commit()
    assert n == 0                                       # 首次落表无事实可挂
    q = conn.execute("SELECT qid, q_norm_hash FROM questions WHERE doc_hash=?", (h1,)).fetchone()
    conn.execute("INSERT INTO label_facts (q_norm_hash, topic, qtype, difficulty, tags, merged_at) "
                 "VALUES (?,?,?,?,?,?)", (q["q_norm_hash"], "Redis", "八股", 3, '["缓存"]', "now"))
    conn.commit()
    old_qid = q["qid"]
    # 模拟全量重切：同 doc 再 store（DELETE+INSERT，qid 换新，q_norm_hash 不变）
    n2 = extract.store(conn, h1, rows1)
    conn.commit()
    assert n2 == 1                                      # 重挂 1 题
    row = conn.execute("SELECT qid, topic, qtype, difficulty, tags FROM questions "
                       "WHERE doc_hash=?", (h1,)).fetchone()
    assert row["qid"] != old_qid                        # qid 确实漂移（B1 根因）
    assert row["topic"] == "Redis" and row["qtype"] == "八股"
    assert row["difficulty"] == 3 and row["tags"] == '["缓存"]'


def test_main_reports_label_facts_lost(tmp_env, capsys):
    """main 输出 label_reattached / label_facts_lost：重切后真正丢失的打标要可见。"""
    corpus, conn = tmp_env
    _seed_doc(conn, corpus, "a.md", MD_A)
    conn.execute("INSERT INTO label_facts (q_norm_hash, topic, merged_at) "
                 "VALUES ('deadbeefdeadbeef', 'X', 'now')")
    conn.commit()
    rc = extract.main(["--db", str(config.DB_PATH)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["label_reattached"] == 0
    assert out["label_facts_lost"] == 1                 # facts 里的 hash 已无对应题


def test_extract_empty_doc(tmp_env):
    corpus, conn = tmp_env
    h, p = _seed_doc(conn, corpus, "empty.md", "太短")
    assert extract.extract_doc(h, p) == []


def test_exclusion_pool(tmp_env, tmp_path, monkeypatch):
    """P2-⑤：exclusions.json 命中的源件与 md 镜像都不进题池。"""
    corpus, conn = tmp_env
    h, p = _seed_doc(conn, corpus, "被排除.md", MD_A)
    (config.QUESTION_MD_DIR / "镜像.md").write_text(MD_A, encoding="utf-8")
    ex = tmp_path / "ex.json"
    ex.write_text(json.dumps({"items": [
        {"rel": "被排除.md", "md_rel": "镜像.md", "reason": "test"}]}, ensure_ascii=False),
        encoding="utf-8")
    monkeypatch.setattr(config, "EXCLUSIONS_FILE", ex)
    pool = extract.doc_pool(conn)
    paths = {v.name for _, v in pool}
    assert "被排除.md" not in paths and "镜像.md" not in paths
    monkeypatch.setattr(config, "EXCLUSIONS_FILE", tmp_path / "不存在.json")
    paths2 = {v.name for _, v in extract.doc_pool(conn)}
    assert "被排除.md" in paths2 and "镜像.md" in paths2


def test_pool_hash_stable_across_content_change(tmp_env, monkeypatch):
    """md 镜像 doc_hash 按路径稳定：md 内容被改写（OCR 合并/懒加载）hash 不变，不产生孤儿行。"""
    corpus, conn = tmp_env
    md_dir = config.QUESTION_MD_DIR
    p = md_dir / "子目录"
    p.mkdir()
    f = p / "题库.md"
    f.write_text("# 第一版\n\n" + "1. 什么是索引？" * 5, encoding="utf-8")
    key1 = next(k for k, v in extract.doc_pool(conn) if v == f)
    f.write_text("# 第二版（内容变了，懒加载改写）\n\n" + "1. 什么是索引？" * 5, encoding="utf-8")
    key2 = next(k for k, v in extract.doc_pool(conn) if v == f)
    assert key1 == key2
    # 孤儿清理：写入一个不在池里的 doc_hash 的题行，purge 应删掉
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
        "q_norm, q_norm_hash, simhash, extracted_at) VALUES (?, 1, 'x', '', 0, 1, 'x', 'x', 0, 'now')",
        ("deadbeefdeadbeef",))
    conn.commit()
    removed = extract.purge_orphans(conn, {key2})
    assert removed == 1
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash='deadbeefdeadbeef'").fetchone()[0] == 0


def test_pool_skips_old_md_when_hash_sibling_exists(tmp_env):
    """旧 x.md 与 reconvert 生成的 x_<hash8>.md 共存时，只抽带哈希的新版，避免重复计题。"""
    corpus, conn = tmp_env
    md_dir = config.QUESTION_MD_DIR
    (md_dir / "题库").mkdir(parents=True)
    old = md_dir / "题库" / "x.md"
    new = md_dir / "题库" / "x_abcd1234.md"
    old.write_text(MD_A, encoding="utf-8")
    new.write_text(MD_A, encoding="utf-8")
    names = {v.name for _, v in extract.doc_pool(conn)}
    assert new.name in names and old.name not in names
