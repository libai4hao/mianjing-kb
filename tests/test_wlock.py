"""跨进程写锁（T15）：互斥、持有者可见、陈旧锁可接管。"""
import json

import pytest

from tools.kb import wlock


def test_lock_roundtrip(tmp_path):
    with wlock.write_lock("kb", tool="t", build_dir=tmp_path) as p:
        assert p.exists()
        assert json.loads(p.read_text(encoding="utf-8"))["tool"] == "t"
    assert not p.exists()


def test_second_writer_is_rejected_with_holder_info(tmp_path):
    with wlock.write_lock("kb", tool="intake", build_dir=tmp_path):
        with pytest.raises(wlock.LockHeld) as ei:
            with wlock.write_lock("kb", tool="purge", build_dir=tmp_path):
                pass
    assert "intake" in str(ei.value)
    assert ei.value.holder.get("tool") == "intake"


def test_stale_lock_from_dead_pid_is_taken_over(tmp_path, capsys):
    d = wlock.lock_dir(tmp_path)
    (d / "kb.lock").write_text(json.dumps({"pid": 999999999, "tool": "ghost",
                                           "started_at": "2026-01-01T00:00:00"}),
                               encoding="utf-8")
    with wlock.write_lock("kb", tool="intake", build_dir=tmp_path):
        pass                      # 不抛异常：PID 已不在，接管成功
    assert "接管陈旧锁" in capsys.readouterr().err


def test_wait_then_timeout_raises(tmp_path):
    with wlock.write_lock("kb", tool="a", build_dir=tmp_path):
        with pytest.raises(wlock.LockHeld):
            with wlock.write_lock("kb", tool="b", build_dir=tmp_path, wait_sec=0.3):
                pass
