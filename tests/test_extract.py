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
    # 跳过不代表清零：未重抽文档的题数必须原样保留。
    assert conn.execute("SELECT questions_n FROM document_products "
                        "WHERE doc_hash=?", (h1,)).fetchone()["questions_n"] == 2

    # 新文件 → 只处理 1 篇
    _seed_doc(conn, corpus, "b.md", MD_B)
    assert extract.main(["--db", str(config.DB_PATH), "--incremental"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 1 and out["skipped_unchanged"] == 1
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 4


def test_incremental_reextracts_changed_mapped_md_after_mapping_sync(tmp_env, capsys):
    """映射扫描不能抢先覆盖成功抽取指纹；即使 size/mtime 不变，也要识别 MD 内容变化。"""
    import hashlib
    import os
    from mianjing_kb.kb import products

    corpus, conn = tmp_env
    source = corpus / "a.pdf"
    source.write_bytes(b"source-placeholder")
    doc_hash = "a1b2c3d4e5f60718"
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (doc_hash, hashlib.sha256(b"source-placeholder").hexdigest(), "a.pdf", ".pdf",
         source.stat().st_size, str(source), "", "classified", "t", "now", "now"))
    md = config.QUESTION_MD_DIR / "题库" / "a.md"
    md.parent.mkdir(parents=True)
    md.write_text(MD_A, encoding="utf-8")
    conn.commit()

    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    capsys.readouterr()
    before = products.get(conn, doc_hash)
    assert before["text_source"] == "md"
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash=?", (doc_hash,)).fetchone()[0] == 2

    # 保持字节长度与 mtime 不变，验证 md_hash 才是最终兜底指纹。
    old_stat = md.stat()
    md.write_text(MD_A.replace("Redis", "Kafka"), encoding="utf-8")
    assert md.stat().st_size == old_stat.st_size
    os.utime(md, (old_stat.st_atime, old_stat.st_mtime))

    # 与 extract.main 的映射同步阶段相同：不能把“待处理的新指纹”写成“上次成功指纹”。
    from mianjing_kb.kb import mdmap
    mapping = mdmap.build(conn, config.QUESTION_MD_DIR)
    products.sync_mapping(conn, mapping)
    conn.commit()
    after_sync = products.get(conn, doc_hash)
    assert after_sync["md_hash"] == before["md_hash"]
    assert after_sync["input_size"] == before["input_size"]
    assert products.needs_work(conn, doc_hash, md) is True

    assert extract.main(["--db", str(config.DB_PATH), "--incremental"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 1 and out["skipped_unchanged"] == 0
    questions = conn.execute("SELECT q_text FROM questions WHERE doc_hash=?", (doc_hash,)).fetchall()
    assert questions and any("Kafka" in r["q_text"] for r in questions)
    assert products.get(conn, doc_hash)["md_hash"] != before["md_hash"]


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
    # 限量抽取同样不得把未处理文档的题数清零。
    rows = conn.execute("SELECT questions_n FROM document_products").fetchall()
    assert len(rows) == 2 and all(r["questions_n"] == 2 for r in rows)


def test_doc_list_processes_only_listed_docs(tmp_env, tmp_path, capsys):
    """--doc-list 定向补切：只动清单里的 doc，别的不重切；孤儿清理必须跳过。"""
    corpus, conn = tmp_env
    h1, _ = _seed_doc(conn, corpus, "a.md", MD_A)
    h2, _ = _seed_doc(conn, corpus, "b.md", MD_B)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    capsys.readouterr()
    # a 的题先留一条"孤儿"哨兵，验证 doc-list 运行也不会删它
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, "
        "q_norm, q_norm_hash, simhash, extracted_at) VALUES (?, 1, 'x', '', 0, 1, 'x', 'x', 0, 'now')",
        ("deadbeefdeadbeef",))
    conn.commit()
    lst = tmp_path / "docs.txt"
    lst.write_text(f"# 只补 b\n{h2}\nnot-a-real-hash\n", encoding="utf-8")
    assert extract.main(["--db", str(config.DB_PATH), "--doc-list", str(lst)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 1
    assert out["purge_skipped"] == "doc-list"
    assert out["purged_orphan_docs"] == 0
    assert out["skipped_doc_list_missing"] == 1
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash='deadbeefdeadbeef'"
                        ).fetchone()[0] == 1
    # 只重切了 b：b 仍是 2 题，a 的 2 题原样保留
    per = dict(conn.execute("SELECT doc_hash, COUNT(*) FROM questions GROUP BY 1").fetchall())
    assert per.get(h1) == 2 and per.get(h2) == 2


def test_doc_list_preserves_llm_answers_by_hash(tmp_env, tmp_path, capsys):
    corpus, conn = tmp_env
    h1, p1 = _seed_doc(conn, corpus, "a.md", MD_A)
    h2, _ = _seed_doc(conn, corpus, "b.md", MD_B)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    conn.execute("UPDATE questions SET a_text='模型补答正文', answer_source='llm', answer_model='m', "
                 "answered_at='t' WHERE doc_hash=? AND seq=2", (h1,))
    p1.write_text(MD_A.replace("纯内存操作加 IO 多路复用，单线程模型避免了锁竞争，减少了上下文切换开销。", ""),
                  encoding="utf-8")
    conn.commit()
    capsys.readouterr()
    lst = tmp_path / "docs.txt"
    lst.write_text(f"{h1}\n{h2}\n", encoding="utf-8")

    assert extract.main(["--db", str(config.DB_PATH), "--doc-list", str(lst)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 2 and out["answer_provenance_skipped"] == 0
    assert out["answer_provenance_lost"] == 0
    row = conn.execute("SELECT a_text, answer_source FROM questions WHERE doc_hash=? AND seq=2",
                       (h1,)).fetchone()
    assert row["a_text"] == "模型补答正文" and row["answer_source"] == "llm"

    assert extract.main(["--db", str(config.DB_PATH), "--doc-list", str(lst),
                         "--allow-answer-loss"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["docs"] == 2 and out["answer_provenance_lost"] == 0
    assert conn.execute("SELECT a_text FROM questions WHERE doc_hash=? AND seq=2", (h1,)
                        ).fetchone()[0] == "模型补答正文"
    assert extract.main(["--db", str(config.DB_PATH), "--plan"]) == 0
    assert json.loads(capsys.readouterr().out)["answer_provenance_docs"] == 1


def test_plan_reports_answer_provenance_docs(tmp_env, capsys):
    """只读 --plan 把"带补答溯源的文档数"报出来，补切前先看风险面。"""
    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "a.md", MD_A)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    conn.execute("UPDATE questions SET answer_source='llm_refused' WHERE doc_hash=? AND seq=1", (h,))
    conn.commit()
    capsys.readouterr()
    assert extract.main(["--db", str(config.DB_PATH), "--plan"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["answer_provenance_docs"] == 1 and out["pool_after_gate"] == 1


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


def test_extract_cleans_bold_numbering_but_keeps_identity_and_answers(tmp_env):
    """F4：题干洗掉 `**` 与题号，q_norm/q_key 仍按原串（身份不漂移），答案一字不动。"""
    corpus, conn = tmp_env
    h, p = _seed_doc(conn, corpus, "bold.md",
                     "**1** 、什么是 **Redis** ?\n\nRedis 是内存数据库，指针 int** p 不该被改。\n"
                     + "补充说明。" * 20)
    rows = extract.extract_doc(h, p)
    assert len(rows) == 1
    assert rows[0]["q_text"] == "什么是 Redis?"
    assert "**" not in rows[0]["q_text"]
    assert "int** p" in rows[0]["a_text"]                  # 代码块/答案不受影响
    assert rows[0]["q_norm"].startswith("1")                # 身份口径仍是原串（题号保留）


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



def test_negative_limit_is_rejected_before_database_access(tmp_env):
    """ISSUE-07：--limit -1 会被切片解释成"除最后一件外全做"，必须在解析后立刻拒绝。"""
    with pytest.raises(SystemExit) as exc:
        extract.main(["--db", str(config.DB_PATH), "--limit", "-1", "--dry-run"])
    assert exc.value.code == 2


def test_zero_limit_still_means_unlimited(tmp_env):
    """0 保持"不限制"语义，不能被误拦。"""
    assert extract.main(["--db", str(config.DB_PATH), "--limit", "0", "--dry-run"]) == 0


def test_store_preserves_answers_state_and_labels_by_hash(tmp_env):
    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "preserve.md", MD_A)
    rows = extract.extract_doc(h, corpus / "preserve.md")
    rows[0]["a_text"] = ""
    extract.store(conn, h, rows)
    old = conn.execute(
        "SELECT qid, q_norm_hash FROM questions WHERE doc_hash=? ORDER BY qid LIMIT 1", (h,)
    ).fetchone()
    conn.execute(
        "UPDATE questions SET a_text='LLM 答案', answer_state='ok', answer_source='llm', "
        "answer_model='m', answered_at='t', topic='Redis', qtype='算法', difficulty=4, "
        "tags='[\"缓存\"]', position='[\"后端\"]', quality='[\"残缺\"]', canonical_q='标准问句' "
        "WHERE qid=?", (old["qid"],))
    conn.commit()

    extract.store(conn, h, rows)
    row = conn.execute(
        "SELECT qid, a_text, answer_state, answer_source, answer_model, answered_at, topic, "
        "qtype, difficulty, tags, position, quality, canonical_q FROM questions "
        "WHERE doc_hash=? AND q_norm_hash=?", (h, old["q_norm_hash"])).fetchone()

    assert row["qid"] != old["qid"]
    assert tuple(row)[1:6] == ("LLM 答案", "ok", "llm", "m", "t")
    assert tuple(row)[6:] == ("Redis", "算法", 4, '["缓存"]', '["后端"]', '["残缺"]', "标准问句")


def test_store_new_source_answer_replaces_old_llm_answer(tmp_env):
    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "new-answer.md", MD_A)
    rows = extract.extract_doc(h, corpus / "new-answer.md")
    extract.store(conn, h, rows)
    conn.execute(
        "UPDATE questions SET a_text='旧模型答案', answer_state='no_answer_source', "
        "answer_source='llm', answer_model='m', answered_at='t' WHERE doc_hash=? AND seq=1", (h,))
    conn.commit()
    rows[0]["a_text"] = "本次源文档答案"

    extract.store(conn, h, rows)
    row = conn.execute(
        "SELECT a_text, answer_state, answer_source, answer_model, answered_at FROM questions "
        "WHERE doc_hash=? AND seq=1", (h,)).fetchone()

    assert tuple(row) == ("本次源文档答案", "ok", "", None, None)


@pytest.mark.parametrize("source", ["llm_refused", "llm_error"])
def test_store_inherits_empty_answer_terminal_state(tmp_env, source):
    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "empty-answer.md", MD_A)
    rows = extract.extract_doc(h, corpus / "empty-answer.md")
    rows[0]["a_text"] = ""
    extract.store(conn, h, rows)
    old = conn.execute("SELECT qid FROM questions WHERE doc_hash=? AND seq=1", (h,)).fetchone()
    conn.execute(
        "UPDATE questions SET answer_state='no_answer_source', answer_source=?, "
        "answer_model='m', answered_at='t' WHERE qid=?", (source, old["qid"]))
    conn.commit()

    extract.store(conn, h, rows)
    row = conn.execute(
        "SELECT a_text, answer_state, answer_source, answer_model, answered_at FROM questions "
        "WHERE doc_hash=? AND seq=1", (h,)).fetchone()
    assert tuple(row) == ("", "no_answer_source", source, "m", "t")


def test_store_refuses_unmatched_governed_question_and_rolls_back(tmp_env):
    from mianjing_kb.extract import StateLossError

    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "changed.md", MD_A)
    rows = extract.extract_doc(h, corpus / "changed.md")
    extract.store(conn, h, rows)
    old = conn.execute("SELECT qid FROM questions WHERE doc_hash=? AND seq=1", (h,)).fetchone()
    conn.execute(
        "UPDATE questions SET a_text='已治理答案', answer_state='ok', answer_source='llm', "
        "answer_model='m', answered_at='t' WHERE qid=?", (old["qid"],))
    conn.commit()
    before = [tuple(r) for r in conn.execute(
        "SELECT * FROM questions WHERE doc_hash=? ORDER BY qid", (h,))]
    changed = [dict(r) for r in rows]
    changed[0]["q_norm_hash"] = "different-hash"

    with pytest.raises(StateLossError, match="allow-state-loss"):
        extract.store(conn, h, changed)
    assert [tuple(r) for r in conn.execute(
        "SELECT * FROM questions WHERE doc_hash=? ORDER BY qid", (h,))] == before

    extract.store(conn, h, changed, allow_state_loss=True)
    assert conn.execute(
        "SELECT COUNT(*) FROM questions WHERE doc_hash=? AND a_text='已治理答案'", (h,)
    ).fetchone()[0] == 0


def test_store_allows_unmatched_source_answer_and_derived_state(tmp_env):
    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "source-only.md", MD_A)
    rows = extract.extract_doc(h, corpus / "source-only.md")
    extract.store(conn, h, rows)
    conn.execute("UPDATE questions SET answer_state='boundary_gap' WHERE doc_hash=? AND seq=1", (h,))
    conn.commit()
    changed = [dict(r) for r in rows]
    changed[0]["q_norm_hash"] = "changed-source-hash"

    extract.store(conn, h, changed)
    assert conn.execute(
        "SELECT COUNT(*) FROM questions WHERE doc_hash=? AND q_norm_hash=?",
        (h, "changed-source-hash")).fetchone()[0] == 1


@pytest.mark.parametrize("field,value", [
    ("topic", "Redis"),
    ("qtype", "算法"),
    ("difficulty", 4),
    ("tags", '["缓存"]'),
    ("position", '["后端"]'),
    ("quality", '["残缺"]'),
    ("canonical_q", "标准问句"),
])
def test_store_refuses_unmatched_governance_field(tmp_env, field, value):
    from mianjing_kb.extract import StateLossError

    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "governed.md", MD_A)
    rows = extract.extract_doc(h, corpus / "governed.md")
    extract.store(conn, h, rows)
    old = conn.execute("SELECT qid FROM questions WHERE doc_hash=? AND seq=1", (h,)).fetchone()
    conn.execute(f"UPDATE questions SET {field}=? WHERE qid=?", (value, old["qid"]))
    conn.commit()
    before = [tuple(r) for r in conn.execute(
        "SELECT * FROM questions WHERE doc_hash=? ORDER BY qid", (h,))]
    changed = [dict(r) for r in rows]
    changed[0]["q_norm_hash"] = "different-hash"

    with pytest.raises(StateLossError, match="allow-state-loss"):
        extract.store(conn, h, changed)
    assert [tuple(r) for r in conn.execute(
        "SELECT * FROM questions WHERE doc_hash=? ORDER BY qid", (h,))] == before

    loss_report = {}
    extract.store(conn, h, changed, allow_state_loss=True, loss_report=loss_report)
    assert loss_report["count"] == 1 and loss_report["provenance_count"] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM questions WHERE doc_hash=? AND q_norm_hash=?",
        (h, "different-hash")).fetchone()[0] == 1


def test_store_refuses_to_orphan_last_label_fact(tmp_env):
    from mianjing_kb.extract import StateLossError

    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "fact.md", MD_A)
    rows = extract.extract_doc(h, corpus / "fact.md")
    extract.store(conn, h, rows)
    old_hash = rows[0]["q_norm_hash"]
    conn.execute("UPDATE questions SET a_text='', answer_state=NULL WHERE doc_hash=? AND seq=1", (h,))
    conn.execute(
        "INSERT INTO label_facts (q_norm_hash, topic, merged_at) VALUES (?, 'Redis', 'now')",
        (old_hash,))
    conn.commit()
    changed = [dict(r) for r in rows]
    changed[0]["q_norm_hash"] = "new-hash"

    with pytest.raises(StateLossError, match="label_facts 1 条"):
        extract.store(conn, h, changed)
    assert conn.execute("SELECT topic FROM label_facts WHERE q_norm_hash=?", (old_hash,)
                        ).fetchone()[0] == "Redis"


def test_store_allows_orphan_fact_when_another_document_keeps_reference(tmp_env):
    corpus, conn = tmp_env
    h1, _ = _seed_doc(conn, corpus, "fact-a.md", MD_A)
    h2, _ = _seed_doc(conn, corpus, "fact-b.md", MD_B)
    rows1 = extract.extract_doc(h1, corpus / "fact-a.md")
    rows2 = extract.extract_doc(h2, corpus / "fact-b.md")
    extract.store(conn, h1, rows1)
    extract.store(conn, h2, rows2)
    shared = {r["q_norm_hash"] for r in rows1} & {r["q_norm_hash"] for r in rows2}
    old_hash = next(iter(shared))
    conn.execute("UPDATE questions SET a_text='', answer_state=NULL "
                 "WHERE doc_hash=? AND q_norm_hash=?", (h1, old_hash))
    conn.execute(
        "INSERT INTO label_facts (q_norm_hash, topic, merged_at) VALUES (?, 'Redis', 'now')",
        (old_hash,))
    conn.commit()
    changed = [dict(r) for r in rows1]
    next(r for r in changed if r["q_norm_hash"] == old_hash)["q_norm_hash"] = "new-hash"

    extract.store(conn, h1, changed)
    assert conn.execute("SELECT topic FROM label_facts WHERE q_norm_hash=?", (old_hash,)
                        ).fetchone()[0] == "Redis"


def test_store_refuses_conflicting_duplicate_hash(tmp_env):
    from mianjing_kb.extract import StateLossError

    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "duplicate.md", MD_A)
    rows = extract.extract_doc(h, corpus / "duplicate.md")
    extract.store(conn, h, rows)
    first = rows[0]
    conn.execute("DROP INDEX uq_questions_qkey")
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, span_start, span_end, q_norm, "
        "q_norm_hash, simhash, extracted_at, q_key) VALUES (?,99,?,?,0,1,?,?,0,'now',?)",
        (h, first["q_text"], "冲突答案", first["q_norm"], first["q_norm_hash"], first["q_key"]))
    conn.commit()

    with pytest.raises(StateLossError, match="冲突"):
        extract.store(conn, h, rows)


def test_store_accepts_plain_sqlite_connection(tmp_env):
    import sqlite3

    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "plain.md", MD_A)
    rows = extract.extract_doc(h, corpus / "plain.md")
    raw = sqlite3.connect(str(config.DB_PATH))
    try:
        assert extract.store(raw, h, rows) == 0
    finally:
        raw.close()


def test_purge_orphans_refuses_governed_rows_without_explicit_authorization(tmp_env):
    from mianjing_kb.extract import StateLossError

    _corpus, conn = tmp_env
    conn.execute(
        "INSERT INTO questions (doc_hash, seq, q_text, a_text, answer_state, answer_source, "
        "q_norm, q_norm_hash, simhash, span_start, span_end, extracted_at) "
        "VALUES ('stale',1,'旧题','治理答案','ok','llm','旧题','old-hash',0,0,1,'now')")
    conn.commit()

    with pytest.raises(StateLossError):
        extract.purge_orphans(conn, set())
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash='stale'").fetchone()[0] == 1
    assert extract.purge_orphans(conn, set(), allow_state_loss=True) == 1


def test_purge_orphans_refuses_fact_shared_only_by_multiple_stale_docs(tmp_env):
    from mianjing_kb.extract import StateLossError

    corpus, conn = tmp_env
    h1, _ = _seed_doc(conn, corpus, "stale-a.md", MD_A)
    h2, _ = _seed_doc(conn, corpus, "stale-b.md", MD_B)
    rows1 = extract.extract_doc(h1, corpus / "stale-a.md")
    rows2 = extract.extract_doc(h2, corpus / "stale-b.md")
    extract.store(conn, h1, rows1)
    extract.store(conn, h2, rows2)
    shared = {r["q_norm_hash"] for r in rows1} & {r["q_norm_hash"] for r in rows2}
    old_hash = next(iter(shared))
    conn.execute(
        "INSERT INTO label_facts (q_norm_hash, topic, merged_at) VALUES (?, 'Redis', 'now')",
        (old_hash,))
    conn.commit()

    with pytest.raises(StateLossError, match="label_facts 1 条"):
        extract.purge_orphans(conn, set())
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash IN (?,?)",
                        (h1, h2)).fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM label_facts WHERE q_norm_hash=?",
                        (old_hash,)).fetchone()[0] == 1


def test_main_reports_structured_state_loss_and_returns_nonzero(tmp_env, monkeypatch, capsys):
    corpus, conn = tmp_env
    h, p = _seed_doc(conn, corpus, "loss.md", MD_A)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    rows = extract.extract_doc(h, p)
    old = conn.execute("SELECT qid FROM questions WHERE doc_hash=? AND seq=1", (h,)).fetchone()
    conn.execute("UPDATE questions SET a_text='治理答案', answer_source='llm' WHERE qid=?",
                 (old["qid"],))
    conn.commit()
    before = [tuple(r) for r in conn.execute(
        "SELECT * FROM questions WHERE doc_hash=? ORDER BY qid", (h,))]
    changed = [dict(r) for r in rows]
    changed[0]["q_norm_hash"] = "different-hash"
    real_extract = extract.extract_doc_status
    monkeypatch.setattr(extract, "extract_doc_status",
                        lambda doc_hash, path: (changed, "ok") if path == p else real_extract(doc_hash, path))
    capsys.readouterr()

    assert extract.main(["--db", str(config.DB_PATH)]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["state_loss_blocked"] == 1
    assert out["state_loss"][0]["qids"] == [old["qid"]]
    assert [tuple(r) for r in conn.execute(
        "SELECT * FROM questions WHERE doc_hash=? ORDER BY qid", (h,))] == before

    assert extract.main(["--db", str(config.DB_PATH), "--allow-state-loss"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["state_loss_blocked"] == 0 and out["answer_provenance_lost"] == 1


def test_allow_empty_overwrite_authorizes_state_loss(tmp_env, capsys):
    corpus, conn = tmp_env
    h, p = _seed_doc(conn, corpus, "empty-loss.md", MD_A)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    conn.execute("UPDATE questions SET answer_source='llm' WHERE doc_hash=? AND seq=1", (h,))
    p.write_text("太短", encoding="utf-8")
    conn.commit()
    capsys.readouterr()

    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    assert json.loads(capsys.readouterr().out)["empty_preserved"] == 1
    assert extract.main(["--db", str(config.DB_PATH), "--allow-empty-overwrite"]) == 0
    capsys.readouterr()
    assert conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash=?", (h,)).fetchone()[0] == 0


def test_plan_detects_answer_model_without_source(tmp_env, capsys):
    corpus, conn = tmp_env
    h, _ = _seed_doc(conn, corpus, "model-only.md", MD_A)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0
    conn.execute("UPDATE questions SET answer_model='m' WHERE doc_hash=? AND seq=1", (h,))
    conn.commit()
    capsys.readouterr()

    assert extract.main(["--db", str(config.DB_PATH), "--plan"]) == 0
    assert json.loads(capsys.readouterr().out)["answer_provenance_docs"] == 1
