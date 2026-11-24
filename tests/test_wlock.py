# -*- coding: utf-8 -*-
"""跨进程 OS 写锁：互斥、持有者可见、进程崩溃自动释放。"""
import json
import multiprocessing
import time

import pytest

from mianjing_kb.kb import config, wlock


def _hold_lock(build_dir, ready, release):
    with wlock.write_lock("kb", tool="child-process", build_dir=build_dir):
        ready.set()
        release.wait(10)


def test_target_lock_name_distinguishes_configured_online_database(tmp_path, monkeypatch):
    build = tmp_path / "build"
    monkeypatch.setattr(config, "BUILD_DIR", build)
    monkeypatch.setattr(config, "DB_PATH", build / "kb.sqlite")
    assert wlock.lock_name_for_target(build / "kb.sqlite") == "kb"
    assert wlock.lock_name_for_target(build / "面经库.db") == "online"
    assert wlock.lock_name_for_target(tmp_path / "custom" / "kb.sqlite") == "kb"
    assert wlock.lock_name_for_target(tmp_path / "custom" / "online.db") == "kb"


def test_lock_roundtrip_keeps_lock_file_and_metadata(tmp_path):
    with wlock.write_lock("kb", tool="t", build_dir=tmp_path) as p:
        assert p.exists()
        assert wlock._read_holder(p)["tool"] == "t"
    # 不删除锁文件，避免 unlink/recreate 导致不同进程锁住不同 inode。
    assert p.exists()
    assert wlock._read_holder(p)["tool"] == "t"


def test_second_writer_is_rejected_with_holder_info(tmp_path):
    with wlock.write_lock("kb", tool="intake", build_dir=tmp_path):
        with pytest.raises(wlock.LockHeld) as ei:
            with wlock.write_lock("kb", tool="purge", build_dir=tmp_path):
                pass
    assert "intake" in str(ei.value)
    assert ei.value.holder.get("tool") == "intake"


def test_dead_pid_metadata_does_not_require_rename_takeover(tmp_path):
    """锁所有权由 OS 决定；无活跃 OS 锁时，陈旧 PID 元数据可直接覆盖。"""
    d = wlock.lock_dir(tmp_path)
    p = d / "kb.lock"
    p.write_bytes(b"\0" + json.dumps({"pid": 999999999, "tool": "ghost"}).encode())
    with wlock.write_lock("kb", tool="new-owner", build_dir=tmp_path):
        assert wlock._read_holder(p)["tool"] == "new-owner"
    assert not list(d.glob("kb.lock.stale-*"))


def test_unreadable_or_half_written_metadata_does_not_steal_active_lock(tmp_path):
    with wlock.write_lock("kb", tool="owner", build_dir=tmp_path) as p:
        # 破坏诊断元数据不会释放 OS 锁，也不会让其他写者接管。
        with p.open("r+b") as f:
            f.seek(1)
            f.write(b"not-json")
            f.truncate()
        with pytest.raises(wlock.LockHeld):
            with wlock.write_lock("kb", tool="other", build_dir=tmp_path):
                pass


def test_cross_process_writer_is_rejected(tmp_path):
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    proc = ctx.Process(target=_hold_lock, args=(str(tmp_path), ready, release))
    proc.start()
    try:
        assert ready.wait(8), "child did not acquire lock in time"
        with pytest.raises(wlock.LockHeld) as ei:
            with wlock.write_lock("kb", tool="parent", build_dir=tmp_path, wait_sec=0.1):
                pass
        assert ei.value.holder.get("tool") == "child-process"
    finally:
        release.set()
        proc.join(8)
        if proc.is_alive():
            proc.terminate()
            proc.join(3)
    assert proc.exitcode == 0


def test_wait_then_timeout_raises(tmp_path):
    with wlock.write_lock("kb", tool="a", build_dir=tmp_path):
        started = time.monotonic()
        with pytest.raises(wlock.LockHeld):
            with wlock.write_lock("kb", tool="b", build_dir=tmp_path, wait_sec=0.2):
                pass
        assert time.monotonic() - started >= 0.15
