# -*- coding: utf-8 -*-
"""extract（P1-1 题块落表）回归测试：临时语料 + 临时库，端到端。"""
import json
from pathlib import Path

import pytest

from mianjing_kb import extract
from mianjing_kb.kb import config


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
    from mianjing_kb.kb import db as kbdb
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

纯内存操作加 IO 多路复用，单线程模型避免了锁竞争，减少了上下文切换开销。
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
    pool, _mapping = extract.doc_pool(conn)
    paths = {v.name for _, v in pool}
    assert "被排除.md" not in paths and "镜像.md" not in paths
    monkeypatch.setattr(config, "EXCLUSIONS_FILE", tmp_path / "不存在.json")
    pool2, _m2 = extract.doc_pool(conn)
    paths2 = {v.name for _, v in pool2}
    assert "被排除.md" in paths2 and "镜像.md" in paths2


def test_pool_hash_stable_across_content_change(tmp_env, monkeypatch):
    """md 镜像 doc_hash 按路径稳定：md 内容被改写（OCR 合并/懒加载）hash 不变，不产生孤儿行。"""
    corpus, conn = tmp_env
    md_dir = config.QUESTION_MD_DIR
    p = md_dir / "子目录"
    p.mkdir()
    f = p / "题库.md"
    f.write_text("# 第一版\n\n" + "1. 什么是索引？" * 5, encoding="utf-8")
    pool1, _m1 = extract.doc_pool(conn)
    key1 = next(k for k, v in pool1 if v == f)
    f.write_text("# 第二版（内容变了，懒加载改写）\n\n" + "1. 什么是索引？" * 5, encoding="utf-8")
    pool2, _m2 = extract.doc_pool(conn)
    key2 = next(k for k, v in pool2 if v == f)
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
    pool, _m = extract.doc_pool(conn)
    names = {v.name for _, v in pool}
    assert new.name in names and old.name not in names


def test_incremental_skips_unchanged_and_picks_new(tmp_env, capsys):
    """Q-3：增量按 document_products 的 size/mtime 跳过未变件；新件照常处理。"""
    corpus, conn = tmp_env
    h1, _ = _seed_doc(conn, corpus, "a.md", MD_A)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 1 and out["incremental"] is False
    st = conn.execute("SELECT status FROM source_documents WHERE doc_hash=?", (h1,)).fetchone()[0]
    assert st == "processed"
    assert conn.execute("SELECT questions_n, extracted_at FROM document_products "
                        "WHERE doc_hash=?", (h1,)).fetchone()["questions_n"] == 2

    # 第二次增量：没变 → 全跳过
    assert extract.main(["--db", str(config.DB_PATH), "--incremental"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 0 and out["skipped_unchanged"] == 1

    # 新文件 → 只处理 1 篇
    _seed_doc(conn, corpus, "b.md", MD_B)
    assert extract.main(["--db", str(config.DB_PATH), "--incremental"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 1 and out["skipped_unchanged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 4


def test_exclusion_source_rel_suppresses_mapped_md(tmp_env, tmp_path, monkeypatch):
    """源文件 rel 命中排除表就整篇不抽，即使 md 产物本身未被排除。"""
    corpus, conn = tmp_env
    _seed_doc(conn, corpus, "a.md", MD_A)
    (config.QUESTION_MD_DIR / "a.md").write_text(MD_A, encoding="utf-8")
    ex = tmp_path / "ex_src.json"
    ex.write_text(json.dumps({"items": [{"rel": "a.md", "reason": "test"}]},
                             ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "EXCLUSIONS_FILE", ex)
    pool, _m = extract.doc_pool(conn)
    assert pool == []


def test_non_ingested_md_mirror_does_not_reenter_pool(tmp_env):
    """隔离/废弃源留下的 md 产物不能被当成孤儿重新进池。"""
    corpus, conn = tmp_env
    h, _p = _seed_doc(conn, corpus, "q.md", MD_A)
    conn.execute("UPDATE source_documents SET status='quarantined' WHERE doc_hash=?", (h,))
    conn.commit()
    (config.QUESTION_MD_DIR / "q.md").write_text(MD_A, encoding="utf-8")
    pool, _m = extract.doc_pool(conn)
    assert pool == []


def test_limit_skips_purge(tmp_env, capsys):
    """--limit 是抽样试跑，不能把未处理文档的题当成孤儿清掉。"""
    corpus, conn = tmp_env
    _seed_doc(conn, corpus, "a.md", MD_A)
    _seed_doc(conn, corpus, "b.md", MD_B)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    capsys.readouterr()
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
        "q_norm, q_norm_hash, simhash, extracted_at) VALUES (?, 1, 'x', '', 0, 1, 'x', 'x', 0, 'now')",
        ("deadbeefdeadbeef",))
    conn.commit()
    assert extract.main(["--db", str(config.DB_PATH), "--limit", "1"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["purge_skipped"] == "limit" and out["purged_orphan_docs"] == 0
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash='deadbeefdeadbeef'"
                        ).fetchone()[0] == 1


def test_many_read_errors_skip_purge(tmp_env, capsys):
    """报错文档超过 5% 阈值时也不能清理，避免把未处理部分误判为孤儿。"""
    corpus, conn = tmp_env
    _seed_doc(conn, corpus, "a.md", MD_A)
    _seed_doc(conn, corpus, "b.md", MD_B)
    for i in range(2):
        bad = corpus / f"bad{i}.md"
        bad.mkdir()
        h = f"bad{i:08d}"
        conn.execute(
            "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
            "dir_path, status, batch_id, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (h, h, bad.name, ".md", 1, str(bad), "", "classified", "t", "now", "now"))
    conn.commit()
    # 有文档解析失败：即使做了保护性跳过 purge，退出码也必须非零
    assert extract.main(["--db", str(config.DB_PATH)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["errors"] == 2 and out["purge_skipped"] == "errors"
    assert out["purged_orphan_docs"] == 0


def test_empty_result_preserves_old_questions(tmp_env, capsys):
    """已有题的文档本次 0 题：默认保留旧题并告警，显式 --allow-empty-overwrite 才清空。"""
    corpus, conn = tmp_env
    h, p = _seed_doc(conn, corpus, "a.md", MD_A)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    capsys.readouterr()
    p.write_text("太短", encoding="utf-8")
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["empty_preserved"] == 1
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash=?", (h,)).fetchone()[0] == 2
    assert extract.main(["--db", str(config.DB_PATH), "--allow-empty-overwrite"]) == 0
    capsys.readouterr()
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash=?", (h,)).fetchone()[0] == 0


def test_stats_does_not_create_missing_db(tmp_env, tmp_path):
    """--stats 路径写错必须报错，不能悄悄建一个空库。"""
    with pytest.raises(FileNotFoundError):
        extract.main(["--stats", "--db", str(tmp_path / "不存在" / "kb.sqlite")])
    assert not (tmp_path / "不存在" / "kb.sqlite").exists()


def test_plan_does_not_write_identity_report(tmp_env, capsys):
    """--plan 是只读模式：不落 md_identity_report.json。"""
    corpus, conn = tmp_env
    _seed_doc(conn, corpus, "a.md", MD_A)
    assert extract.main(["--plan", "--db", str(config.DB_PATH)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["report"] is None
    assert not (config.BUILD_DIR / "md_identity_report.json").exists()


def test_extract_heading_numbered_questions(tmp_env):
    """保留 `## 2.` 后，chunker 能把标题题与上一题分开（不再当列表项）。"""
    corpus, conn = tmp_env
    h, p = _seed_doc(conn, corpus, "head.md",
                     "1. 什么是TCP？\n\nTCP 是面向连接的协议。\n\n"
                     "## 2. 什么是UDP？\n\nUDP 无连接，开销小，适合实时传输。\n" + "补充说明。" * 20)
    rows = extract.extract_doc(h, p)
    assert [r["seq"] for r in rows] == [1, 2]
    assert "UDP" in rows[1]["q_text"]


def test_full_run_keeps_processed_docs(tmp_env, capsys):
    """processed 不能把文档踢出题池：全量重跑仍要覆盖，且不 purge 已处理的题。"""
    corpus, conn = tmp_env
    _seed_doc(conn, corpus, "a.md", MD_A)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    capsys.readouterr()
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 1 and out["purged_orphan_docs"] == 0
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 2


def test_text_of_gb18030_and_bom(tmp_env):
    """GB18030 md 不能解成 U+FFFD；UTF-8 BOM 不能留在首行。"""
    corpus, _conn = tmp_env
    gb = corpus / "gb.md"
    gb.write_bytes(("# 题\n\n" + "1. 什么是缓存穿透？\n\n" + "大量不存在的键查询打到数据库。" * 8)
                   .encode("gb18030"))
    text = extract.text_of(gb)
    assert text and "\ufffd" not in text and "缓存穿透" in text
    bom = corpus / "bom.md"
    bom.write_text("\ufeff# 题\n\n" + "1. 什么是索引？\n\n" + "B+ 树按页组织数据。" * 10,
                   encoding="utf-8")
    text = extract.text_of(bom)
    assert text and not text.startswith("\ufeff") and "索引" in text


def test_text_of_nfkc(tmp_env):
    """康熙部首/全角字符先 NFKC 再入库，避免检索与去重被异体字打断。"""
    corpus, _conn = tmp_env
    p = corpus / "nfkc.md"
    p.write_text("1. ⼤⼚的缓存怎么设计？\n\n" + "分片、副本与热点隔离。" * 12, encoding="utf-8")
    text = extract.text_of(p)
    assert text and "⼤" not in text and "大厂" in text


def test_text_of_preserves_heading_markers(tmp_env):
    """text_of 不再剥 #：chunker 要靠标题块/R9/块间空行识别题目。"""
    corpus, _conn = tmp_env
    p = corpus / "head.md"
    text = ("para\n\n## 2. 什么是UDP\n\nUDP 无连接，开销小。" * 8
            + "\n\n# 供服务自动注册、自动发现等高效服务治理方案\n\n正文")
    p.write_text(text, encoding="utf-8")
    out = extract.text_of(p)
    assert out and "## 2." in out
    assert "para\n\n## 2." in out.replace("\r\n", "\n")
    assert "# 供服务" not in out and "供服务自动注册" in out

