# -*- coding: utf-8 -*-
"""文本读取：gate / extract 共用的唯一解码顺序。

utf-8-sig 必须先试：普通 utf-8 也能读带 BOM 的文件，但会把 U+FEFF 留在正文。
gb18030 作为第二候选；两者都失败才用 replace 兜底，并记录实际编码，避免静默错解。
"""
from __future__ import annotations

from pathlib import Path


def read_text_info(path: Path) -> tuple[str, str]:
    """返回 (正文, 实际编码)。"""
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8-replace"


def read_text(path: Path) -> str:
    return read_text_info(path)[0]
