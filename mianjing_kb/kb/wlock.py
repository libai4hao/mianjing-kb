# -*- coding: utf-8 -*-
"""跨进程数据库写锁。

锁文件使用操作系统 advisory lock（Unix: flock；Windows: msvcrt.locking）。
锁文件本身永久保留，进程退出/崩溃时由 OS 自动释放锁；不再通过读取 PID 后
rename/unlink 陈旧锁文件，避免 TOCTOU 接管竞争及 unlink 后双持有者问题。
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config

LOCK_DIR_NAME = "locks"
_LOCAL_GUARD = threading.Lock()
_LOCAL_LOCKS: dict[str, threading.Lock] = {}


class LockHeld(RuntimeError):
    """锁被另一个线程或进程持有。"""

    def __init__(self, path: Path, holder: dict | None):
        self.path = path
        self.holder = holder or {}
        pid = self.holder.get("pid", "?")
        tool = self.holder.get("tool", "?")
        started = self.holder.get("started_at", "?")
        target = self.holder.get("target", "?")
        super().__init__(f"写锁被占用：{path.name}（pid={pid} tool={tool} "
                         f"目标库={target} 起始={started}）")


def lock_dir(build_dir: Path | str | None = None) -> Path:
    d = Path(build_dir or config.BUILD_DIR) / LOCK_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _local_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCAL_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


def _read_holder(p: Path) -> dict | None:
    """读取锁文件元数据；首字节为 OS 锁占位字节。"""
    try:
        with p.open("rb") as f:
            f.seek(1)
            raw = f.read().decode("utf-8", errors="replace").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {"raw": raw}
    except json.JSONDecodeError:
        return {"raw": raw}


def _try_os_lock(fd: int) -> bool:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    except OSError as ex:
        if ex.errno in (11, 13):
            return False
        raise


def inspect_lock(path: Path | str) -> tuple[bool | None, dict | None]:
    """检查 advisory lock 是否正被持有，不创建/删除锁文件，也不改写元数据。

    返回 (locked, holder)：False 表示文件存在但当前无人持锁；True 表示已被持有；
    None 表示无法可靠探测（例如权限问题）。永久保留的锁文件本身不代表活跃锁。
    """
    p = Path(path)
    if not p.exists():
        return False, None
    local = _local_lock(p)
    if local.locked():
        return True, _read_holder(p)

    fd = None
    try:
        flags = os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(p), flags)
        if _try_os_lock(fd):
            _unlock(fd)
            return False, None
        return True, _read_holder(p)
    except FileNotFoundError:
        return False, None
    except OSError:
        return None, _read_holder(p)
    finally:
        if fd is not None:
            os.close(fd)


def _unlock(fd: int) -> None:
    if sys.platform == "win32":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


@contextmanager
def write_lock(name: str = "kb", tool: str = "?", *, build_dir: Path | str | None = None,
               wait_sec: float = 0.0, target: str | Path | None = None):
    """获取跨进程排他锁。

    OS 锁是唯一所有权事实；PID/工具名只用于诊断。锁文件不删除，避免进程 A
    解锁/删除与进程 B 新建同名锁之间出现 inode/file-object 分裂。
    """
    d = lock_dir(build_dir)
    p = d / f"{name}.lock"
    local = _local_lock(p)
    deadline = time.monotonic() + max(0.0, wait_sec)
    local_wait = max(0.0, deadline - time.monotonic())
    if not local.acquire(timeout=local_wait):
        raise LockHeld(p, _read_holder(p))

    fd = None
    acquired = False
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        fd = os.open(str(p), flags, 0o600)
        # Windows byte-range locking 需要文件至少有一个字节；该字节不承载 JSON。
        if os.fstat(fd).st_size < 1:
            os.write(fd, b"\0")
            os.fsync(fd)
        while not _try_os_lock(fd):
            if time.monotonic() >= deadline:
                raise LockHeld(p, _read_holder(p))
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
        acquired = True

        metadata = {
            "pid": os.getpid(), "tool": tool,
            "target": str(target) if target else ("kb.sqlite" if name == "kb" else name),
            "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        }
        payload = json.dumps(metadata, ensure_ascii=False).encode("utf-8")
        os.lseek(fd, 1, os.SEEK_SET)
        os.write(fd, payload)
        os.ftruncate(fd, 1 + len(payload))
        os.fsync(fd)
        yield p
    finally:
        if fd is not None:
            if acquired:
                try:
                    _unlock(fd)
                except OSError:
                    pass
            os.close(fd)
        local.release()
