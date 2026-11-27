# -*- coding: utf-8 -*-
"""P0-1 单身份回归：md 产物不带自己的身份，题行一律用源 doc_hash。

覆盖：hash8 精确映射、唯一 stem、歧义裁决（非 md 源优先）、旧/新同名 md、多产物选主、
extract 端到端只留一种身份、build_db + G8（双份检测）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from mianjing_kb import build_db as bdb
from mianjing_kb import extract, verify_ledger as vl
from mianjing_kb.kb import config, db as kbdb, mdmap

Q_TEXT = """# 资料

1. 什么是 Redis 持久化？

RDB 快照与 AOF 日志两种方式，RDB 按策略定期落盘，AOF 追加写命令日志。

2. Redis 为什么这么快？

纯内存操作加 IO 多路复用。
"""


@pytest.fixture()
def env(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    build.mkdir(parents=True)
    (corpus / "20-题库MD").mkdir()
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    monkeypatch.setattr(config, "QUESTION_MD_DIR", corpus / "20-题库MD")
    monkeypatch.setattr(config, "EXCLUSIONS_FILE", tmp_path / "no-exclusions.json")
    conn = kbdb.connect(build / "kb.sqlite")
    yield corpus, conn
    conn.close()


def _seed(conn, corpus: Path, rel: str, text: str) -> str:
    p = corpus / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (h, h, p.name, p.suffix, len(text), str(p), str(p.parent), "classified", "t", "now", "now"))
    conn.commit()
    return h


def _md(corpus: Path, rel: str, text: str) -> Path:
    p = corpus / "20-题库MD" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_hash8_mapping_and_single_pool(env):
    corpus, conn = env
    h = _seed(conn, corpus, "src/题库/Spring/甲.txt", "源文件正文" * 30)
    md = _md(corpus, f"题库/Spring/甲__{h[:8]}.md", Q_TEXT)

    mapping = mdmap.build(conn, config.QUESTION_MD_DIR)
    assert mapping.by_doc[h][0] == f"题库/Spring/甲__{h[:8]}.md"
    assert mapping.report["by_hash8"] == 1 and mapping.report["orphans"] == 0

    pool, mp = extract.doc_pool(conn)
    assert [k for k, _p in pool] == [h]                 # 只有源身份
    assert dict(pool)[h] == md
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0


def test_unique_stem_maps_without_hash_suffix(env):
    corpus, conn = env
    h = _seed(conn, corpus, "src/题库/JVM/乙.txt", "源" * 60)
    _md(corpus, "题库/JVM/乙.md", Q_TEXT)
    pool, mp = extract.doc_pool(conn)
    assert [k for k, _p in pool] == [h]
    assert mp.report["by_stem"] == 1


def _seed_bytes(conn, corpus: Path, rel: str, text: str) -> str:
    """按落盘后的真实字节登记 doc_hash（Windows 文本写入会转 CRLF，
    与 intake 的"对实际字节哈希"口径一致；_seed 的文本哈希只适用于无换行内容）。"""
    p = corpus / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    h = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    conn.execute(
        "INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, src_path, "
        "dir_path, status, batch_id, first_seen_at, last_seen_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (h, h, p.name, p.suffix, p.stat().st_size, str(p), str(p.parent),
         "classified", "t", "now", "now"))
    conn.commit()
    return h


def test_ambiguous_stem_requires_content_match(env):
    """#36：同名 a.pdf / a.md 两个源 + 无后缀 md——只有字节一致的源才认领产物。

    产物字节 == a.md 源字节 → 归属 a.md；a.pdf 回退抽自己的源文件。
    旧口径"非 md 源优先"会让 a.pdf 冒领 a.md 的正文（错误归属）。
    """
    corpus, conn = env
    h_pdf = _seed_bytes(conn, corpus, "src/题库/x/a.pdf", "pdf 内容" * 30)
    h_md = _seed_bytes(conn, corpus, "src/题库/x/a.md", Q_TEXT)
    _md(corpus, "题库/x/a.md", Q_TEXT)

    mapping = mdmap.build(conn, config.QUESTION_MD_DIR)
    assert mapping.by_doc == {h_md: ("题库/x/a.md", config.QUESTION_MD_DIR / "题库/x/a.md")}
    assert mapping.report["conflicts"][0]["chosen"] == h_md
    assert mapping.report["conflicts"][0]["rule"] == "content_hash_match"
    pool, _ = extract.doc_pool(conn)
    assert set(dict(pool)) == {h_pdf, h_md}
    assert dict(pool)[h_pdf] == corpus / "src/题库/x/a.pdf"   # 回退自己的源文件


def test_ambiguous_stem_without_content_match_keeps_orphan(env):
    """#36：产物与任何候选源字节都不一致 → 不猜归属，产物留孤儿、各源抽自己。"""
    corpus, conn = env
    h_pdf = _seed_bytes(conn, corpus, "src/题库/x/b.pdf", "pdf 内容" * 30)
    h_md = _seed_bytes(conn, corpus, "src/题库/x/b.md", Q_TEXT)
    _md(corpus, "题库/x/b.md", Q_TEXT + "\n转换后新增的一段。")   # 与两个源都不同

    mapping = mdmap.build(conn, config.QUESTION_MD_DIR)
    assert mapping.by_doc == {}
    assert mapping.report["ambiguous"] == 1
    assert mapping.report["conflicts"][0]["chosen"] is None
    assert mapping.report["conflicts"][0]["rule"] == "unresolved_kept_orphan"
    assert len(mapping.orphans) == 1                     # 产物以 md-mirror 身份保留
    pool, _ = extract.doc_pool(conn)
    keys = set(dict(pool))
    assert h_pdf in keys and h_md in keys                # 两源都回退自己的文件
    assert dict(pool)[h_pdf] == corpus / "src/题库/x/b.pdf"
    assert dict(pool)[h_md] == corpus / "src/题库/x/b.md"
    assert len(keys) == 3                                # 孤儿产物单独成池，内容不丢


def test_old_md_dropped_when_hashed_sibling_exists(env):
    corpus, conn = env
    h = _seed(conn, corpus, "src/题库/JVM/c.txt", "源" * 60)
    old = _md(corpus, "题库/JVM/c.md", Q_TEXT)
    new = _md(corpus, f"题库/JVM/c__{h[:8]}.md", Q_TEXT + "\n补充一段。")

    pool, mp = extract.doc_pool(conn)
    assert dict(pool)[h] == new
    old_rel = old.relative_to(config.QUESTION_MD_DIR).as_posix()
    assert old_rel in {d.get("md") or d.get("dropped") for d in mp.report["duplicates"]}
    assert mp.report["old_siblings_dropped"] == 1


def test_multi_product_single_identity(env):
    """同一源两份带 hash 的产物：只取主产物，其余记 duplicates（不进题池）。"""
    corpus, conn = env
    h = _seed(conn, corpus, "src/题库/Spring/d.txt", "源" * 60)
    small = _md(corpus, f"题库/Spring/d__{h[:8]}.md", "1. 短问题？\n\n短答案。")
    big = _md(corpus, f"面经/综合/d__{h[:8]}.md", Q_TEXT + "\n3. 第三题？\n\n第三答案。" * 3)

    pool, mp = extract.doc_pool(conn)
    assert dict(pool)[h] == big
    assert mp.report["docs_multi_md"] == 1
    assert any(d["dropped"] == small.relative_to(config.QUESTION_MD_DIR).as_posix()
               for d in mp.report["duplicates"])


def test_extract_end_to_end_single_identity(env, capsys):
    corpus, conn = env
    h = _seed(conn, corpus, "src/题库/JVM/e.txt", "源文件里没有题号。" * 10)
    _md(corpus, f"题库/JVM/e__{h[:8]}.md", Q_TEXT)

    rc = extract.main(["--db", str(config.DB_PATH)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["identity"]["md_mapped_sources"] == 1
    hashes = {r[0] for r in conn.execute("SELECT DISTINCT doc_hash FROM questions")}
    assert hashes == {h}, "只允许源身份，不得再出现 md-mirror 身份"
    assert (Path(config.BUILD_DIR) / "md_identity_report.json").exists()
    # 抽的是 md 正文（有题号），不是源文件
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] >= 2


def test_plan_does_not_write(env, capsys):
    corpus, conn = env
    h = _seed(conn, corpus, "src/题库/JVM/f.txt", "源" * 60)
    _md(corpus, f"题库/JVM/f__{h[:8]}.md", Q_TEXT)
    rc = extract.main(["--db", str(config.DB_PATH), "--plan"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["pool_docs"] == 1 and out["orphan_md"] == 0
    assert conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 0


def test_build_db_and_g8_pass(env):
    corpus, conn = env
    h = _seed(conn, corpus, "src/题库/JVM/g.txt", "源" * 60)
    _md(corpus, f"题库/JVM/g__{h[:8]}.md", Q_TEXT)
    assert extract.main(["--db", str(config.DB_PATH)]) == 0

    out_db = Path(config.BUILD_DIR) / "面经库.db"
    bdb.build(out_db=out_db)
    out = sqlite3.connect(f"file:{out_db.as_posix()}?mode=ro", uri=True)
    out.row_factory = sqlite3.Row
    dc = out.execute("SELECT md_rel, doc_hash FROM doc_contents").fetchall()
    assert len(dc) == 1 and dc[0]["doc_hash"] == h
    assert {r[0] for r in out.execute("SELECT DISTINCT doc_hash FROM questions")} == {h}
    g8 = vl.g8_single_identity(out)
    out.close()
    assert g8["state"] == "PASS" and g8["detail"]["mapped_products"] == 1


def test_src_path_outside_corpus_or_link_is_skipped(env, capsys):
    """#81：台账 src_path 指向语料外 / 软链 → 不进题池（不读未授权正文）。"""
    corpus, conn = env
    h_out = _seed(conn, corpus, "src/题库/JVM/out.txt", "源" * 60)
    outside = corpus.parent / "outside.txt"
    outside.write_text("语料外正文", encoding="utf-8")
    conn.execute("UPDATE source_documents SET src_path=? WHERE doc_hash=?",
                 (str(outside), h_out))
    h_link = _seed(conn, corpus, "src/题库/JVM/link.txt", "不一样的源内容" * 40)
    link = corpus / "src/题库/JVM/link.txt"
    link.unlink()
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlink not permitted on this host")
    conn.commit()
    pool, _ = extract.doc_pool(conn)
    keys = {k for k, _p in pool}
    assert h_out not in keys and h_link not in keys
    assert "越界或为链接" in capsys.readouterr().err
