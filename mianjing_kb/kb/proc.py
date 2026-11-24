# -*- coding: utf-8 -*-
"""子进程硬超时：把不可控的第三方调用（Word COM、转换器）关进独立进程。

为什么需要（待办 T16 / 评审 2026-11-19 P2）：Word COM 的 `Documents.Open/SaveAs2`
没有进程级超时，一份异常文档（加密、损坏、弹窗）会让整个批次卡死；COM 调用也无法被
`signal.alarm` 打断。唯一可靠的止损方式是：**一个文件一个子进程，父进程等超时就杀进程树**。

约定：
- 子进程输出走管道，超时后先杀树再回收，避免僵尸；
- 返回 `(returncode, stdout, stderr, timed_out)`；超时时 `returncode` 为杀进程后的退出码；
- 只做进程管理，不解析业务结果——业务结果由子进程写文件，父进程读文件判定（进程被杀时文件不完整，
  天然区分"超时"与"失败"）。
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys

CREATE_NEW_GROUP = 0x00000200 if os.name == "nt" else 0   # CREATE_NEW_PROCESS_GROUP


def _kill_tree(pid: int) -> None:
    """杀整个进程树：Word 由 COM 拉起，是子进程的子进程，只杀直接子进程会留孤儿 WINWORD。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       capture_output=True, text=True, errors="ignore")
    else:
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def run_with_timeout(argv, timeout: float, cwd: str | os.PathLike | None = None,
                     env: dict | None = None) -> tuple[int, str, str, bool]:
    """跑一个子进程，超过 `timeout` 秒杀进程树并返回 timed_out=True。

    `timeout<=0` 表示不限时（仍会等待结束）。
    """
    kwargs = {
        "cwd": str(cwd) if cwd else None,
        "env": env,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NEW_GROUP
    else:
        kwargs["start_new_session"] = True

    proc = subprocess.Popen(list(argv), **kwargs)          # noqa: S603
    timed_out = False
    try:
        out, err = proc.communicate(timeout=timeout if timeout and timeout > 0 else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc.pid)
        try:
            out, err = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:                  # 杀不掉（极端情况）
            proc.kill()
            out, err = "", ""
    return proc.returncode, out or "", err or "", timed_out


def kill_pid(pid: int) -> bool:
    """按 PID 精确清理（用于子进程写下的 Word PID 兜底）。返回是否发出过 kill。"""
    if not pid or pid <= 0:
        return False
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, text=True, errors="ignore")
        return r.returncode == 0
    try:
        os.kill(pid, signal.SIGKILL)
        return True
    except OSError:
        return False


def tail(text: str, limit: int = 400) -> str:
    """子进程 stderr 只留尾部，避免把整段 traceback 灌进日志。"""
    text = (text or "").strip()
    return text[-limit:] if len(text) > limit else text


if __name__ == "__main__":                                  # 自测用：python -m mianjing_kb.kb.proc 3
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    sec = float(sys.argv[1]) if len(sys.argv) > 1 else 2
    print(run_with_timeout([sys.executable, "-c", f"import time; time.sleep({sec})"], 1.0))
