# -*- coding: utf-8 -*-
"""CLI 标准输出编码兜底。

Windows 上 stdout 被重定向/管道捕获时，Python 3.11 默认按 locale（常见 cp1252）
编码；各脚本大量 `print(json.dumps(..., ensure_ascii=False))` 会在这里直接
UnicodeEncodeError，且往往发生在数据库已提交之后。所有 CLI 入口在打印前调用
本函数，把 stdout/stderr 切到 UTF-8；已经是 UTF-8 的流（含 pytest 捕获）不动。
"""
from __future__ import annotations

import sys


def _is_utf8(stream) -> bool:
    enc = (getattr(stream, "encoding", "") or "").lower().replace("-", "").replace("_", "")
    return enc == "utf8"


def configure_utf8_stdio() -> None:
    """幂等：把非 UTF-8 的 stdout/stderr 重配为 UTF-8。无法重配时静默跳过。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or _is_utf8(stream):
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            # 某些被替换的流不支持重配（例如测试捕获对象）；交给调用方原样输出。
            pass
