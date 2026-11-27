# -*- coding: utf-8 -*-
"""文件路径边界：所有来自清单/语料的相对路径都必须先过这里。

拒绝绝对路径、`..`、Windows drive，并校验 resolve 后仍在指定根内。
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath

# Windows 的 junction/挂载点不是 symlink，但带 reparse point 属性（os.walk 会进去）
_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


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


def is_link(path: Path) -> bool:
    """符号链接 / Windows junction / 其它 reparse point——遍历时一律不跟随。"""
    try:
        st = path.lstat()
    except OSError:
        return True                       # 取不到属性就当不可信
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & _REPARSE)


def resolves_inside(root: Path, path: Path) -> bool:
    """path 解析后是否仍在 root 内（junction/symlink 逃逸判定）。"""
    try:
        return Path(path).resolve().is_relative_to(Path(root).resolve())
    except (OSError, ValueError):
        return False


def walk_files(root: Path, suffixes: tuple[str, ...]):
    """遍历 root 下的普通文件：剪掉 junction/软链目录，跳过逃逸文件。

    语料目录里出现 junction 时 os.walk 会一路走进别处（读的是语料外正文、写的是
    语料外路径）；这里统一在入口剪枝，各调用方不必重复实现。
    """
    root_r = Path(root).resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_link(Path(dirpath) / d)]
        for fn in filenames:
            if suffixes and not fn.lower().endswith(suffixes):
                continue
            p = Path(dirpath) / fn
            if is_link(p) or not resolves_inside(root_r, p):
                continue
            yield p
