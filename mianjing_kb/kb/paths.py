# -*- coding: utf-8 -*-
"""文件路径边界：所有来自清单/语料的相对路径都必须先过这里。

拒绝绝对路径、`..`、Windows drive，并校验 resolve 后仍在指定根内。
"""
from __future__ import annotations

import re
from pathlib import Path, PurePosixPath


class UnsafePath(ValueError):
    """路径越界/非法；调用方应拒绝处理并记失败，而不是继续打开文件。"""


def _check_rel(rel: str | Path) -> Path:
    raw = str(rel)
    if not raw or "\x00" in raw:
        raise UnsafePath("空路径或含 NUL")
    # 清单常来自 Windows，必须在 POSIX 测试机上也识别反斜杠穿越。
    if raw.startswith(("\\\\", "//")) or re.match(r"^[A-Za-z]:", raw):
        raise UnsafePath(f"绝对路径不允许: {raw}")
    normalized = raw.replace("\\", "/")
    p = PurePosixPath(normalized)
    if p.is_absolute() or any(part in ("..", "") for part in p.parts):
        raise UnsafePath(f"相对路径含绝对路径、.. 或空段: {raw}")
    if any(":" in part for part in p.parts):  # Windows NTFS alternate data stream
        raise UnsafePath(f"路径段不允许包含冒号/ADS: {raw}")
    return Path(*p.parts)


def safe_join(root: Path, rel: str | Path, *, must_exist: bool = False) -> Path:
    """把 rel 安全地拼到 root 下；符号链接解析后仍须位于 root 内。"""
    root = Path(root).resolve()
    rel_p = _check_rel(rel)
    candidate = root / rel_p
    try:
        resolved = candidate.resolve(strict=must_exist)
    except OSError as ex:
        raise UnsafePath(f"路径无法解析: {rel!r}: {ex}") from ex
    try:
        resolved.relative_to(root)
    except ValueError as ex:
        raise UnsafePath(f"路径越界: {rel!r} -> {resolved}") from ex
    if must_exist and (not resolved.exists() or not resolved.is_file()):
        raise UnsafePath(f"文件不存在或不是普通文件: {rel!r}")
    return resolved
