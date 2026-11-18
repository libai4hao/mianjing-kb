# -*- coding: utf-8 -*-
"""refresh 测试：全链路 PASS→原子切换 / 门禁 FAIL→影子删除线上不动 / 向量回填失败→中止。"""
import hashlib
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools import refresh as rf  # noqa: E402
from tools.kb import config, db as kbdb  # noqa: E402

REAL_META = config.META_DIR
H1 = "a" * 16


@pytest.fixture()
def mini_env(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    build = corpus / "21-面经数据库" / "build"
    md_dir = corpus / "20-题库MD" / "题库" / "综合"
    src_dir = corpus / "02-Java进阶"
    for d in (build, md_dir, src_dir, corpus / "99-重复文件待删"):
        d.mkdir(parents=True)
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    monkeypatch.setattr(config, "QUESTION_MD_DIR", corpus / "20-题库MD")

    (src_dir / "甲.pdf").write_bytes(b"jia")
    (md_dir / "甲.md").write_text("# 甲\n\n什么是 JVM？", encoding="utf-8")

    # 台账：走 db.connect 完整迁移（build_db SELECT 需要 parent_dir/source_count 列）；
    # 1 classified（有题、源在盘）→ G1-G5 应全过（G4 WARN：影子无向量表）
    kb = kbdb.connect(build / "kb.sqlite")
    kb.execute("INSERT INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes, "
               "src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, topic) "
               "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
               (H1, H1 + "0" * 48, "甲.pdf", ".pdf", 3, str(src_dir / "甲.pdf"),
                "02-Java进阶", "classified", "t", "now", "now", "JVM"))
    kb.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, a_text, qtype, conf, q_norm, "
               "q_norm_hash, simhash, span_start, span_end, extracted_at) "
               "VALUES (1, ?, 1, '什么是 JVM？', '答', '八股', 'high', '什么是jvm', 'x', 0, 0, 1, 'now')",
               (H1,))
    kb.commit()

    # 现行线上库（被切换的旧库）：同数据 + 无向量表
    online = sqlite3.connect(build / "面经库.db")
    online.executescript("CREATE TABLE doc_contents(doc_id INTEGER PRIMARY KEY, md_rel TEXT, "
                         "doc_hash TEXT, mirror_hash TEXT, content TEXT);"
                         "CREATE TABLE questions(qid INTEGER PRIMARY KEY, doc_hash TEXT, seq INTEGER, "
                         "q_text TEXT, a_text TEXT, qtype TEXT, conf TEXT, q_norm_hash TEXT, "
                         "topic TEXT, category TEXT);")
    online.execute("INSERT INTO doc_contents (md_rel, doc_hash, mirror_hash, content) VALUES (?,?,?,?)",
                   ("题库/综合/甲.md", H1, "m" * 16, "# 甲"))
    online.execute("INSERT INTO questions (qid, doc_hash, seq, q_text, q_norm_hash, topic, category) "
                   "VALUES (1, ?, 1, '什么是 JVM？', 'x', 'JVM', '题库')", (H1,))
    online.commit()
    online.close()

    yield {"build": build, "online": build / "面经库.db"}
    kb.close()


def _fail_gates(_shadow):
    return [{"id": "G1", "name": "源-库全覆盖", "level": "硬", "state": "FAIL",
             "fails": [1], "detail": {}, "sql": ""}]


def test_full_path_swap(mini_env):
    assert rf.main([]) == 0
    build = mini_env["build"]
    assert not (build / "面经库_shadow.db").exists()          # 影子已换名
    assert mini_env["online"].exists()
    con = sqlite3.connect(str(mini_env["online"]))
    assert con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    con.close()
    # 旧库侧车已清（fixture 里 online 从未带侧车，切换后也不应出现）
    assert not Path(str(mini_env["online"]) + "-wal").exists()


def test_gate_fail_keeps_online(mini_env, monkeypatch, capsys):
    online = mini_env["online"]
    before = hashlib.sha256(online.read_bytes()).hexdigest()
    monkeypatch.setattr(rf, "run_gates", _fail_gates)
    assert rf.main([]) == 1
    out = capsys.readouterr().out
    assert '"verdict": "FAIL"' in out and "线上 面经库.db 未动" in out
    assert hashlib.sha256(online.read_bytes()).hexdigest() == before   # 线上原封不动
    assert not (mini_env["build"] / "面经库_shadow.db").exists()        # 影子已删


def test_vec_error_aborts(mini_env, monkeypatch, capsys):
    online = mini_env["online"]
    before = hashlib.sha256(online.read_bytes()).hexdigest()
    monkeypatch.setattr(rf, "copy_vec", lambda src, dst: "error: boom")
    assert rf.main([]) == 1
    out = capsys.readouterr().out
    assert '"verdict": "ABORT"' in out
    assert hashlib.sha256(online.read_bytes()).hexdigest() == before
    assert not (mini_env["build"] / "面经库_shadow.db").exists()


def _mk_vec_pair(tmp_path, src_hash: str, dst_hash: str):
    """构造 (现行库, 影子库)：现行带 vec_questions（普通表过存在性检查），两边各一题。"""
    src = tmp_path / "old.db"
    dst = tmp_path / "shadow.db"
    s = sqlite3.connect(src)
    s.executescript("CREATE TABLE questions(qid INTEGER PRIMARY KEY, q_norm_hash TEXT);"
                    "CREATE TABLE vec_questions(rowid INTEGER PRIMARY KEY, embedding BLOB);")
    s.execute("INSERT INTO questions VALUES (1, ?)", (src_hash,))
    s.execute("INSERT INTO vec_questions VALUES (1, x'00' * 4)")
    s.commit()
    s.close()
    d = sqlite3.connect(dst)
    d.executescript("CREATE TABLE questions(qid INTEGER PRIMARY KEY, q_norm_hash TEXT);")
    d.execute("INSERT INTO questions VALUES (1, ?)", (dst_hash,))
    d.commit()
    d.close()
    return src, dst


def test_copy_vec_aborts_on_misaligned_qids(tmp_path):
    """F2/B3：向量按 rowid=qid 拷，现行库题行与影子 qid 对不上（hash 不匹配）→ 拒拷。"""
    src, dst = _mk_vec_pair(tmp_path, src_hash="x", dst_hash="changed")
    out = rf.copy_vec(src, dst)
    assert out.startswith("error") and "对齐" in out


def test_copy_vec_qid_missing_in_shadow_aborts(tmp_path):
    """F2/B3：现行库 qid 在影子里不存在（存量库重切续号场景）→ 拒拷。"""
    src, dst = _mk_vec_pair(tmp_path, src_hash="x", dst_hash="x")
    d = sqlite3.connect(dst)
    d.execute("DELETE FROM questions")
    d.commit()
    d.close()
    out = rf.copy_vec(src, dst)
    assert out.startswith("error") and "对齐" in out


def test_copy_vec_aligned_proceeds(tmp_path):
    """对齐通过后不卡在校验（后续成败取决于 sqlite_vec/表结构，与本断言无关）。"""
    src, dst = _mk_vec_pair(tmp_path, src_hash="x", dst_hash="x")
    out = rf.copy_vec(src, dst)
    assert "对齐" not in out
