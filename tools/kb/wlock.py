# -*- coding: utf-8 -*-
"""跨进程写锁（T15）：`<库目录>/locks/<name>.lock`，锁文件里记 PID + 启动时间 + 工具名。

锁放在**被保护数据库的同级目录**（调用方传 `build_dir=Path(db).parent`），
而不是全局 config.BUILD_DIR：两台机器/两个测试库互不阻塞，测试也不会在真实 build
目录里创建锁。

为什么需要：`refresh.lock` 只挡住"两个 refresh"，intake / batch_label --merge /
doc_convert --apply / review_cli 之间仍可能同时写 kb.sqlite。`busy_timeout=10000`
只是让第二个进程**等**，长任务（intake 扫全盘可以跑 30 分钟）会等到超时，然后抛一句
`database is locked` 的栈——用户看不出是谁在占库。

语义：
  - 拿到锁 = 创建锁文件（O_CREAT|O_EXCL，原子）；
  - 锁文件存在且 PID 还活着 → `LockHeld`，调用方打印"谁在跑"并退出；
  - 锁文件存在但 PID 已死（崩溃残留）→ 视为陈旧锁，改名归档后接管（打印一行告警）；
  - 释放 = 删除锁文件；进程被杀时靠陈旧检测兜底，不需要手工删。
"""
from __future__ import annotations

import json
import os
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from . import config

LOCK_DIR_NAME = "locks"
STALE_SUFFIX = ".stale"


class LockHeld(RuntimeError):
    """锁被另一个活着的进程持有。"""

    def __init__(self, path: Path, holder: dict | None):
        self.path = path
        self.holder = holder or {}
        pid = self.holder.get("pid", "?")
        tool = self.holder.get("tool", "?")
        started = self.holder.get("started_at", "?")
        super().__init__(f"写锁被占用：{path.name}（pid={pid} tool={tool} 起始={started}）")


def _pid_alive(pid: int) -> bool:
    """PID 是否还活着。优先 psutil；没有就按平台兜底（不能 import psutil 就装依赖）。"""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil is not None:
        try:
            return psutil.Process(pid).is_running()
        except Exception:                                  # noqa: BLE001
            return False
    if sys.platform == "win32":
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                        # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def lock_dir(build_dir: Path | str | None = None) -> Path:
    d = Path(build_dir or config.BUILD_DIR) / LOCK_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _read_holder(p: Path) -> dict | None:
    try:
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}


@contextmanager
def write_lock(name: str = "kb", tool: str = "?", *, build_dir: Path | str | None = None,
               wait_sec: float = 0.0):
    """拿写锁；拿不到抛 `LockHeld`（调用方决定打印什么、退出码多少）。

    `wait_sec` 只用于"短任务等一会儿"的场景（如 migrate），长任务不要等——
    等 30 分钟不如直接告诉用户谁在跑。
    """
    d = lock_dir(build_dir)
    p = d / f"{name}.lock"

    def _try_acquire() -> bool:
        try:
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            return False
        try:
            os.write(fd, json.dumps({
                "pid": os.getpid(), "tool": tool,
                "started_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            }, ensure_ascii=False).encode("utf-8"))
        finally:
            os.close(fd)
        return True

    deadline = time.monotonic() + max(0.0, wait_sec)
    while True:
        holder = _read_holder(p) if p.exists() else None
        if p.exists() and holder is not None:
            pid = holder.get("pid")
            if isinstance(pid, int) and _pid_alive(pid):
                if time.monotonic() < deadline:
                    time.sleep(0.2)
                    continue
                raise LockHeld(p, holder)
            # 陈旧锁：改名归档（只有一个进程能改名成功），再抢建
            stale = p.with_name(f"{p.name}{STALE_SUFFIX}-{os.getpid()}")
            try:
                os.replace(p, stale)
                print(f"[warn] 接管陈旧锁 {p.name}（pid={pid} 已不在）：{holder}", file=sys.stderr)
                stale.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        if _try_acquire():
            break
        if time.monotonic() >= deadline:
            raise LockHeld(p, _read_holder(p))
        time.sleep(0.2)
    try:
        yield p
    finally:
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass
