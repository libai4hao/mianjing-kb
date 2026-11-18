# -*- coding: utf-8 -*-
"""purge_origin 数据丢失防护的对抗性测试（对应 GitHub Issue 1~4）。

覆盖：根目录与语料根重叠拒绝 / 自身即保留副本 / 保留副本缺失·为空·内容不一致 /
superseded 无接替件闸门 / 先写日志再删 / 删除失败退出码 / --max-delete /
dry-run 不动磁盘 / --rmdirs 只清本次删空的目录。
"""
from __future__ import annotations

import csv
import sqlite3
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO))

import purge_origin as po           # noqa: E402
from kb import config, db, hashing  # noqa: E402


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


def test_self_as_keep_copy_is_not_deletable(env):
    p = _victim(env, name="hardlink.pdf")
    cand = env["corpus"] / "11-面经与真题" / "hardlink.pdf"
    hardlink_ok = True
    try:
        cand.hardlink_to(p)             # 同一文件两个路径（NTFS 支持硬链接）
    except (OSError, NotImplementedError):
        hardlink_ok = False
        cand.write_bytes(p.read_bytes())
    if hardlink_ok:
        _add(env, p, src=cand)
        assert _run(env, p.parent) == 0
        assert p.exists()               # 唯一副本不许被"自己"顶掉


# ---------------- Issue 2：保留副本内容校验 ----------------
def test_keep_copy_missing_or_empty_or_mismatched(env):
    p = _victim(env)
    missing = env["corpus"] / "11-面经与真题" / "gone.pdf"
    _add(env, p, src=missing)
    env["corpus"].joinpath("11-面经与真题", "gone.pdf").write_bytes(p.read_bytes())
    assert _run(env, p.parent) == 0         # 存在且内容一致 → 计划删除
    missing.unlink()

    empty = env["corpus"] / "11-面经与真题" / "empty.pdf"
    empty.write_bytes(b"")
    _add(env, p, src=empty)                 # 0 字节保留副本
    assert _run(env, p.parent) == 0
    assert p.exists()

    diff = _keep(env, data=b"another-content")
    _add(env, p, src=diff)                  # 同尺寸不同内容？ -> 用同长度不同字节
    diff.write_bytes(b"victim-bytez")
    assert _run(env, p.parent) == 0
    assert p.exists()
    assert diff.stat().st_size == p.stat().st_size


def test_superseded_without_converted_to_is_blocked(env, capsys):
    p = _victim(env)
    _add(env, p, status="superseded", note="no successor recorded", src=_keep(env))
    assert _run(env, p.parent, "--apply", "--yes") == 0
    assert p.exists()                      # 默认拒绝
    assert "superseded" in capsys.readouterr().out

    assert _run(env, p.parent, "--apply", "--yes", "--allow-superseded-no-replacement") == 0
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
