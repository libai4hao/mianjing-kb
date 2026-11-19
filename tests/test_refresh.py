# -*- coding: utf-8 -*-
"""refresh 测试：全链路 PASS→原子切换 / 门禁 FAIL→影子删除线上不动 / 向量回填失败→中止。"""
import hashlib
import os
import sqlite3
from pathlib import Path

import pytest
from mianjing_kb import refresh as rf  # noqa: E402
from mianjing_kb.kb import config, db as kbdb  # noqa: E402

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


def test_swap_keeps_rollback_snapshot(mini_env):
    """切换前必须留旧库快照，回滚窗口不能靠"再跑一次 refresh"。"""
    assert rf.main([]) == 0
    snaps = list((mini_env["build"] / rf.ROLLBACK_DIR).glob("面经库-*.db"))
    assert len(snaps) == 1
    con = sqlite3.connect(str(snaps[0]))          # 快照本身是可打开的有效库
    assert con.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    con.close()


def test_snapshot_keeps_only_last_n(tmp_path):
    """快照淘汰按文件名时间戳，不看 mtime（copy/恢复会继承旧 mtime，排序会失真）。"""
    online = tmp_path / "面经库.db"
    con = sqlite3.connect(online)
    con.execute("CREATE TABLE t(x)")
    con.commit()
    con.close()
    d = tmp_path / rf.ROLLBACK_DIR
    d.mkdir()
    for i in range(5):
        p = d / f"面经库-2026010{i}-000000.db"
        p.write_bytes(b"old")
        os.utime(p, (1000, 1000))          # 全部同 mtime：只靠名字也能选出最新 3 份
    rf._snapshot_gc(online, keep=3)
    left = sorted(p.name for p in d.glob("面经库-*.db"))
    assert left == ["面经库-20260102-000000.db", "面经库-20260103-000000.db",
                    "面经库-20260104-000000.db"]


def test_snapshot_uses_backup_api_and_sees_wal(tmp_path):
    """快照必须用 backup API：WAL 里已提交未 checkpoint 的数据也要带上（copy2 会丢）。"""
    online = tmp_path / "面经库.db"
    con = sqlite3.connect(online)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE questions(qid INTEGER)")
    con.execute("INSERT INTO questions VALUES (1)")
    con.commit()                            # 连接保持打开，数据可能还在 -wal 里
    snap = rf._snapshot(online)
    con.close()
    assert snap is not None and snap.exists()
    s = sqlite3.connect(snap)
    assert s.execute("SELECT COUNT(*) FROM questions").fetchone()[0] == 1
    s.close()


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
    monkeypatch.setattr(rf, "copy_vec", lambda src, dst: {"ok": False, "kind": "boom",
                                                          "detail": "boom"})
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
    s.executescript("CREATE TABLE questions(qid INTEGER PRIMARY KEY, q_norm_hash TEXT, "
                    "doc_hash TEXT, q_key TEXT);"
                    "CREATE TABLE vec_questions(rowid INTEGER PRIMARY KEY, embedding BLOB, "
                    "topic TEXT, qtype TEXT, difficulty INTEGER, q_key TEXT);"
                    "CREATE TABLE vec_meta(key TEXT PRIMARY KEY, value TEXT);")
    s.execute("INSERT INTO questions VALUES (1, ?, 'h', ?)", (src_hash, src_hash))
    s.execute("INSERT INTO vec_questions VALUES (1, x'00' * 4, 'JVM', '八股', 3, ?)", (src_hash,))
    from mianjing_kb.embed_questions import question_set_hash
    s.execute("INSERT INTO vec_meta VALUES ('question_set_hash', ?)", (question_set_hash(s),))
    s.commit()
    s.close()
    d = sqlite3.connect(dst)
    d.executescript("CREATE TABLE questions(qid INTEGER PRIMARY KEY, q_norm_hash TEXT, "
                    "doc_hash TEXT, q_key TEXT);")
    d.execute("INSERT INTO questions VALUES (1, ?, 'h', ?)", (dst_hash, dst_hash))
    d.commit()
    d.close()
    return src, dst


def test_copy_vec_aborts_on_misaligned_qids(tmp_path):
    """F2/B3：向量按 rowid=qid 拷，现行库题行与影子 qid 对不上（hash 不匹配）→ 拒拷。"""
    src, dst = _mk_vec_pair(tmp_path, src_hash="x", dst_hash="changed")
    out = rf.copy_vec(src, dst)
    assert out["ok"] is False and out["kind"] == "misaligned" and "不匹配" in out["detail"]


def test_copy_vec_qid_missing_in_shadow_aborts(tmp_path):
    """F2/B3：现行库 qid 在影子里不存在（存量库重切续号场景）→ 拒拷。"""
    src, dst = _mk_vec_pair(tmp_path, src_hash="x", dst_hash="x")
    d = sqlite3.connect(dst)
    d.execute("DELETE FROM questions")
    d.commit()
    d.close()
    out = rf.copy_vec(src, dst)
    assert out["ok"] is False and out["kind"] == "misaligned"


def test_copy_vec_rejects_both_null_q_norm_hash(tmp_path):
    """两侧 q_norm_hash 全为空时，COALESCE 对齐校验会假通过——必须显式拒绝。"""
    src, dst = _mk_vec_pair(tmp_path, src_hash="", dst_hash="")
    out = rf.copy_vec(src, dst)
    assert out["ok"] is False and out["kind"] == "null_hash"


def test_copy_vec_rejects_orphan_vectors(tmp_path):
    src, dst = _mk_vec_pair(tmp_path, src_hash="x", dst_hash="x")
    s = sqlite3.connect(src)
    s.execute("INSERT INTO vec_questions VALUES (99, x'00' * 4, 'JVM', '八股', 3, 'x')")   # rowid 99 无对应题
    s.commit()
    s.close()
    out = rf.copy_vec(src, dst)
    assert out["ok"] is False and out["kind"] == "orphan_vectors"


def test_copy_vec_aligned_not_misaligned(tmp_path):
    """对齐通过后不卡在对齐校验（后续成败取决于 sqlite_vec/表结构）。"""
    src, dst = _mk_vec_pair(tmp_path, src_hash="x", dst_hash="x")
    out = rf.copy_vec(src, dst)
    assert out["ok"] is False and out["kind"] in ("no_sqlite_vec", "error")
    assert "对齐" not in out.get("detail", "")


def _all_pass(_shadow):
    return [{"id": gid, "name": gid, "level": "硬", "state": "PASS",
             "fails": [], "detail": {}, "sql": ""} for gid in rf.REQUIRED_GATES]


def test_unknown_gate_state_fails(mini_env, monkeypatch, capsys):
    """门禁不是黑名单：G2 返回 'ERROR' 这种未知状态必须判失败，不能放行。"""
    online = mini_env["online"]
    before = hashlib.sha256(online.read_bytes()).hexdigest()
    gates = _all_pass(None)
    gates[1]["state"] = "ERROR"

    monkeypatch.setattr(rf, "run_gates", lambda _s: gates)
    assert rf.main([]) == 1
    out = capsys.readouterr().out
    assert '"verdict": "FAIL"' in out and "未知状态" in out
    assert hashlib.sha256(online.read_bytes()).hexdigest() == before


def test_missing_gate_fails(mini_env, monkeypatch):
    monkeypatch.setattr(rf, "run_gates", lambda _s: _all_pass(None)[:3])
    assert rf.main([]) == 1


def test_refresh_lock_refuses_concurrent_run(mini_env, capsys):
    (mini_env["build"] / rf.LOCK_NAME).write_text("99999", encoding="utf-8")
    assert rf.main([]) == 2
    assert "refresh.lock" in capsys.readouterr().out


def test_swap_failure_keeps_shadow_for_retry(mini_env, monkeypatch, capsys):
    monkeypatch.setattr(rf, "run_gates", _all_pass)
    real_replace = rf.os.replace

    def fake(src, dst, *a, **k):
        if Path(dst) == mini_env["online"]:
            raise PermissionError("in use")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(rf.os, "replace", fake)
    assert rf.main([]) == 1
    out = capsys.readouterr().out
    assert "--swap-only" in out and "未动" in out
    assert (mini_env["build"] / rf.SHADOW_NAME).exists()      # 影子留着，重试不用重建
    assert not (mini_env["build"] / rf.LOCK_NAME).exists()    # 锁已释放


def test_swap_only_rejects_unvalidated_shadow(mini_env):
    """没有发布清单的影子库不能凭"--swap-only"直接上线。"""
    import shutil
    shadow = mini_env["build"] / rf.SHADOW_NAME
    shutil.copy2(mini_env["online"], shadow)
    assert rf.main(["--swap-only"]) == 2
    assert shadow.exists()


def test_swap_only_rejects_tampered_shadow(mini_env):
    """清单存在但影子库 sha256 变了：拒绝切换。"""
    import shutil
    shadow = mini_env["build"] / rf.SHADOW_NAME
    shutil.copy2(mini_env["online"], shadow)
    rf._write_publish_manifest(mini_env["build"], shadow, _all_pass(None))
    shadow.write_bytes(shadow.read_bytes() + b"\x00")
    assert rf.main(["--swap-only"]) == 2
    assert shadow.exists()


def test_swap_only_reuses_shadow(mini_env, monkeypatch):
    import shutil
    shadow = mini_env["build"] / rf.SHADOW_NAME
    shutil.copy2(mini_env["online"], shadow)
    rf._write_publish_manifest(mini_env["build"], shadow, _all_pass(None))
    assert rf.main(["--swap-only"]) == 0
    assert not shadow.exists()


def test_sidecar_cleanup_failure_is_warning_not_abort(mini_env, monkeypatch, capsys):
    """os.replace 已成功但 -wal 删不掉时，必须报"已切换"，不能谎报"线上未动"。"""
    monkeypatch.setattr(rf, "run_gates", _all_pass)
    real_unlink = Path.unlink

    def fake(self, *a, **k):
        if str(self).endswith("面经库.db-wal"):
            raise PermissionError("locked sidecar")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", fake)
    assert rf.main([]) == 0
    out = capsys.readouterr().out
    assert '"swapped": true' in out and "warnings" in out and "locked sidecar" in out


def test_refuses_when_kb_lock_held_by_other_writer(mini_env, monkeypatch, capsys):
    """T15：refresh 与 intake/batch_label 共用写锁；别人持锁就别重建影子库。

    否则 refresh 在切换窗口内别人往线上库写的行会被覆盖掉。
    """
    from mianjing_kb.kb import wlock
    monkeypatch.setattr(rf, "KB_LOCK_WAIT_SEC", 0)
    with wlock.write_lock("kb", tool="intake", build_dir=mini_env["build"]):
        assert rf.main([]) == 2
    out = capsys.readouterr().out
    assert "ABORT" in out and "intake" in out
    assert not (mini_env["build"] / rf.LOCK_NAME).exists()   # 自己的锁也要放掉
    assert not (mini_env["build"] / rf.SHADOW_NAME).exists()  # 没动影子库
