# -*- coding: utf-8 -*-
"""purge_origin 数据丢失防护的对抗性测试（对应 GitHub Issue 1~4）。

覆盖：根目录与语料根重叠拒绝 / 自身即保留副本 / 保留副本缺失·为空·内容不一致 /
superseded 无接替件闸门 / 先写日志再删 / 删除失败退出码 / --max-delete /
dry-run 不动磁盘 / --rmdirs 只清本次删空的目录。
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

from mianjing_kb import purge_origin as po           # noqa: E402
from kb import config, db, events, hashing  # noqa: E402


@pytest.fixture()
def env(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus"
    (corpus / "11-面经与真题").mkdir(parents=True)
    build = tmp_path / "build"
    build.mkdir()
    monkeypatch.setattr(config, "CORPUS_ROOT", corpus)
    monkeypatch.setattr(config, "BUILD_DIR", build)
    return {"corpus": corpus, "build": build, "db": tmp_path / "kb.sqlite", "tmp": tmp_path}


def _add(env, path: Path, status="classified", note="", src: Path | None = None) -> None:
    """把 path 登记成一条 source_documents 记录（src 默认指向语料内的同名副本）。"""
    digest = hashing.sha256_file(path)
    conn = db.connect(env["db"])
    conn.execute(
        "INSERT OR REPLACE INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes,"
        " src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (digest[:16], digest, path.name, path.suffix, path.stat().st_size, str(src or path),
         "x", status, "t", "now", "now", note),
    )
    conn.commit()
    conn.close()


def _victim(env, name="a.pdf", data=b"victim-bytes") -> Path:
    root = env["tmp"] / "src"
    root.mkdir(exist_ok=True)
    p = root / name
    p.write_bytes(data)
    return p


def _keep(env, data=b"victim-bytes", name="keep.pdf") -> Path:
    p = env["corpus"] / "11-面经与真题" / name
    p.write_bytes(data)
    return p


def _run(env, root: Path, *extra: str) -> int:
    return po.main(["--roots", str(root), "--db", str(env["db"]),
                    "--log", str(env["build"] / "del.csv"), *extra])


def _reasons(out: str) -> dict:
    """从预览 JSON 里取 kept_reasons（dry-run 一律 rc 0，必须看原因才算真断言）。"""
    import json
    return json.loads(out[out.index("{"):])["kept_reasons"]


# ---------------- Issue 1：根目录与语料根重叠 ----------------
@pytest.mark.parametrize("which", ["same", "inside", "parent"])
def test_refuses_root_overlapping_corpus(env, which):
    p = _victim(env)
    _add(env, p, src=p)
    root = env["tmp"] / "src"          # 默认为语料根的兄弟目录
    corpus = env["corpus"]
    if which == "same":
        # 直接拿语料根当根：语料根内含一个与 p 同内容的登记件
        target = corpus / "11-面经与真题" / "dup.pdf"
        target.write_bytes(p.read_bytes())
        _add(env, target, src=target)
        root = corpus
    elif which == "inside":
        root = corpus / "11-面经与真题"
    else:
        root = corpus.parent            # tmp_path 是语料根的父目录
    assert _run(env, root, "--apply", "--yes") == 2
    assert p.exists()


def test_self_as_keep_copy_is_not_deletable(env, capsys):
    p = _victim(env, name="hardlink.pdf")
    cand = env["corpus"] / "11-面经与真题" / "hardlink.pdf"
    try:
        cand.hardlink_to(p)             # 同一文件两个路径（NTFS 支持硬链接）
    except (OSError, NotImplementedError):
        pytest.skip("当前文件系统不支持硬链接，无法构造 samefile 场景")
    _add(env, p, src=cand)
    assert _run(env, p.parent) == 0
    assert p.exists()                   # 唯一副本不许被"自己"顶掉
    assert _reasons(capsys.readouterr().out).get("keep copy is self") == 1


# ---------------- Issue 2：保留副本内容校验 ----------------
def test_keep_copy_missing_or_empty_or_mismatched(env, capsys):
    p = _victim(env)
    missing = env["corpus"] / "11-面经与真题" / "gone.pdf"
    _add(env, p, src=missing)
    assert _run(env, p.parent) == 0
    assert _reasons(capsys.readouterr().out).get("保留副本不存在") == 1

    empty = env["corpus"] / "11-面经与真题" / "empty.pdf"
    empty.write_bytes(b"")
    _add(env, p, src=empty)
    assert _run(env, p.parent) == 0
    assert _reasons(capsys.readouterr().out).get("保留副本为空") == 1
    assert p.exists()

    diff = env["corpus"] / "11-面经与真题" / "keep.pdf"
    diff.write_bytes(b"victim-bytez")    # 同尺寸、不同字节
    _add(env, p, src=diff)
    assert _run(env, p.parent) == 0
    assert _reasons(capsys.readouterr().out).get("keep copy content mismatch") == 1
    assert p.exists()


def test_superseded_without_converted_to_is_blocked(env, capsys):
    p = _victim(env)
    _add(env, p, status="superseded", note="no successor recorded", src=_keep(env))
    assert _run(env, p.parent, "--apply", "--yes") == 0
    assert p.exists()                      # 默认拒绝
    assert "superseded" in capsys.readouterr().out

    assert _run(env, p.parent, "--apply", "--yes", "--allow-superseded-no-replacement") == 0
    assert not p.exists()


def test_superseded_with_event_converted_to_is_allowed(env):
    """T12：note 里没有 converted_to 子串，但事件表记了接替件 → purge 必须放行。

    旧实现只认 note 子串，事件化之后如果读方不同步，就会把合法转换件永久拒绝。
    """
    p = _victim(env)
    _add(env, p, status="superseded", note="no successor recorded", src=_keep(env))
    conn = db.connect(env["db"], create=False)
    events.append(conn, hashing.sha256_file(p)[:16], events.KIND_CONVERTED_TO,
                  {"dir": "11-面经与真题", "name": "keep.pdf", "hash": "0" * 16})
    conn.commit()
    conn.close()
    assert _run(env, p.parent, "--apply", "--yes") == 0
    assert not p.exists()


# ---------------- Issue 3：删除链路 ----------------
def test_apply_writes_planned_then_deleted_with_full_digest(env):
    p = _victim(env)
    _add(env, p, src=_keep(env))
    assert _run(env, p.parent, "--apply", "--yes") == 0
    assert not p.exists()
    rows = list(csv.DictReader((env["build"] / "del.csv").open(encoding="utf-8-sig")))
    statuses = [r["status"] for r in rows]
    assert statuses == ["planned", "deleted"]      # 日志先于 unlink
    assert all(len(r["doc_hash"]) == 64 for r in rows)


def test_dry_run_changes_nothing(env):
    p = _victim(env)
    _add(env, p, src=_keep(env))
    assert _run(env, p.parent) == 0
    assert p.exists()
    assert not (env["build"] / "del.csv").exists()


def test_unlink_failure_returns_nonzero_and_logs_failed(env, monkeypatch):
    p = _victim(env)
    _add(env, p, src=_keep(env))
    real_unlink = Path.unlink

    def boom(self, *a, **k):
        if self == p:
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", boom)
    assert _run(env, p.parent, "--apply", "--yes") == 1
    assert p.exists()
    rows = list(csv.DictReader((env["build"] / "del.csv").open(encoding="utf-8-sig")))
    assert [r["status"] for r in rows] == ["planned", "failed"]


def test_max_delete_guard(env):
    p = _victim(env, name="a.pdf")
    p2 = _victim(env, name="b.pdf", data=b"other")
    _add(env, p, src=_keep(env, name="k1.pdf"))
    _add(env, p2, src=_keep(env, data=b"other", name="k2.pdf"))
    assert _run(env, p.parent, "--apply", "--yes", "--max-delete", "1") == 2
    assert p.exists() and p2.exists()              # 超限一件都不删
    assert _run(env, p.parent, "--apply", "--yes", "--max-delete", "2") == 0
    assert not p.exists() and not p2.exists()


# ---------------- Issue 4：rmdir 只清本次删空的 ----------------
def test_rmdirs_only_removes_dirs_emptied_by_this_run(env):
    root = env["tmp"] / "src"
    _victim(env, name="a.pdf")
    (root / "already-empty").mkdir()
    target = root / "sub"
    target.mkdir()
    sub_file = target / "b.pdf"
    sub_file.write_bytes(b"sub")
    _add(env, sub_file, src=_keep(env, data=b"sub", name="k2.pdf"))
    _add(env, root / "a.pdf", src=_keep(env, name="k1.pdf"))
    assert _run(env, root, "--apply", "--yes", "--rmdirs") == 0
    assert (root / "already-empty").exists()       # 原本就空的目录不碰
    assert not target.exists()                     # 本次删空 → 清掉


def test_rehash_detects_content_swapped_between_scan_and_delete(env, monkeypatch):
    """扫描到删除之间文件被换掉（size/mtime 都伪造成一样）→ 重算 sha256 拦下，不删。"""
    p = _victim(env)
    _add(env, p, src=_keep(env))
    real = hashing.sha256_file

    def fake(path):
        if Path(path) == p and fake.calls:
            return "0" * 64
        if Path(path) == p:
            fake.calls += 1
        return real(path)

    fake.calls = 0
    monkeypatch.setattr(po.hashing, "sha256_file", fake)
    assert _run(env, p.parent, "--apply", "--yes") == 1
    assert p.exists()
    rows = list(csv.DictReader((env["build"] / "del.csv").open(encoding="utf-8-sig")))
    assert [r["status"] for r in rows] == ["planned", "failed"]


def test_no_rehash_flag_skips_the_second_hash(env, monkeypatch):
    p = _victim(env)
    _add(env, p, src=_keep(env))
    real = hashing.sha256_file

    def fake(path):
        if Path(path) == p and fake.calls:
            return "0" * 64
        if Path(path) == p:
            fake.calls += 1
        return real(path)

    fake.calls = 0
    monkeypatch.setattr(po.hashing, "sha256_file", fake)
    assert _run(env, p.parent, "--apply", "--yes", "--no-rehash") == 0
    assert not p.exists()


def test_legacy_log_header_is_rotated(env):
    """老日志是 5 列，直接追加 6 列行会撑坏 CSV：表头不符时改写 _v2，旧文件一个字节不动。"""
    p = _victim(env)
    _add(env, p, src=_keep(env))
    legacy = env["build"] / "del.csv"
    legacy.write_text("deleted_at,origin_path,size_bytes,doc_hash,kept_copy\n", encoding="utf-8")
    before = legacy.read_bytes()
    assert _run(env, p.parent, "--apply", "--yes") == 0
    assert legacy.read_bytes() == before
    rows = list(csv.DictReader((env["build"] / "del_v2.csv").open(encoding="utf-8-sig")))
    assert [r["status"] for r in rows] == ["planned", "deleted"]


def test_db_is_opened_readonly(env):
    p = _victim(env)
    _add(env, p, src=_keep(env))
    before = env["db"].stat().st_mtime_ns
    assert _run(env, p.parent) == 0
    assert env["db"].stat().st_mtime_ns == before
    conn = db.connect(env["db"], create=False, readonly=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM source_documents")
    conn.close()


def test_keep_copy_in_quarantine_is_refused(env):
    """评审 2026-11-19：99-重复文件待删 里的副本不能当保留副本（它本身就是待删件）。"""
    qdir = env["corpus"] / "99-重复文件待删"
    qdir.mkdir(parents=True)
    keep = qdir / "a.pdf"
    keep.write_bytes(b"victim-bytes")
    victim = _victim(env)
    _add(env, victim, status="classified", src=keep)

    rc = _run(env, victim.parent, "--apply", "--yes", "--no-backup")
    assert rc == 0                 # 计划 0 件、实删 0 件：一致
    assert victim.exists()

def test_short_hash_collision_is_refused(env, capsys):
    """16 位短指纹撞上另一份内容的登记行：不得删原件（完整 sha256 是最终判据）。"""
    victim = _victim(env)
    digest = hashing.sha256_file(victim)
    conn = db.connect(env["db"])
    conn.execute(
        "INSERT OR REPLACE INTO source_documents (doc_hash, sha256, file_name, ext, size_bytes,"
        " src_path, dir_path, status, batch_id, first_seen_at, last_seen_at, note)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (digest[:16], "0" * 64, "other.pdf", ".pdf", 1, str(_keep(env)),
         "x", "classified", "t", "now", "now", ""),
    )
    conn.commit()
    conn.close()

    rc = _run(env, victim.parent, "--apply", "--yes", "--no-backup")
    assert rc == 0
    assert victim.exists()


# ---------------- 候选 #52：删前复验保留副本身份/内容 ----------------

def test_keep_copy_swapped_after_plan_is_refused(env, monkeypatch):
    """#52：计划通过后保留副本被改写（扫描期缓存的哈希不再可信）→ 拒删。"""
    p = _victim(env)
    keep = _keep(env)
    _add(env, p, src=keep)
    real = hashing.sha256_file
    state = {"planned": False}

    def fake(path):
        # 第一次对 keep 的哈希是计划期；之后 keep 已被换内容
        if Path(path) == keep and state["planned"]:
            return real(path)
        if Path(path) == keep:
            state["planned"] = True
            digest = real(path)
            keep.write_bytes(b"swapped-after-plan")   # 计划落定后立刻换内容
            return digest
        return real(path)

    monkeypatch.setattr(po.hashing, "sha256_file", fake)
    assert _run(env, p.parent, "--apply", "--yes", "--no-backup") == 1
    assert p.exists()                                 # 原件保住了
    rows = list(csv.DictReader((env["build"] / "del.csv").open(encoding="utf-8-sig")))
    assert [r["status"] for r in rows] == ["planned", "failed"]


def test_keep_copy_replaced_by_symlink_is_refused(env, monkeypatch):
    """#52：保留副本在删前被换成指向外部的软链 → 拒删（链接不是可信保留副本）。"""
    p = _victim(env)
    keep = _keep(env)
    _add(env, p, src=keep)
    outside = env["tmp"] / "outside.pdf"
    outside.write_bytes(b"victim-bytes")
    real = hashing.sha256_file

    def fake(path):
        digest = real(path)
        if Path(path) == keep and not fake.done:
            fake.done = True
            keep.unlink()
            try:
                keep.symlink_to(outside)
            except (OSError, NotImplementedError):
                pytest.skip("symlink not permitted on this host")
        return digest

    fake.done = False
    monkeypatch.setattr(po.hashing, "sha256_file", fake)
    assert _run(env, p.parent, "--apply", "--yes", "--no-backup") == 1
    assert p.exists()


def test_origin_replaced_by_symlink_is_refused(env, monkeypatch):
    """#52：待删件在删前被换成软链 → lstat/链接检查拒删，不跟随删除外部目标。"""
    p = _victim(env)
    _add(env, p, src=_keep(env))
    outside = env["tmp"] / "outside-target.pdf"
    outside.write_bytes(b"outside-sentinel")
    real = hashing.sha256_file

    def fake(path):
        digest = real(path)
        if Path(path) == p and fake.calls == 1:
            p.unlink()
            try:
                p.symlink_to(outside)
            except (OSError, NotImplementedError):
                pytest.skip("symlink not permitted on this host")
        if Path(path) == p:
            fake.calls += 1
        return digest

    fake.calls = 0
    monkeypatch.setattr(po.hashing, "sha256_file", fake)
    rc = _run(env, p.parent, "--apply", "--yes", "--no-backup")
    assert rc == 1
    assert outside.read_bytes() == b"outside-sentinel"   # 外部目标未被删
    assert p.is_symlink()                                # 链接本身也未被跟随处理
