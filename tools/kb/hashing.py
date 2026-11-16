# -*- coding: utf-8 -*-
"""指纹：字节指纹（一级判重）与内容指纹（二级同源判定）。

实测基线（444 件 / 2,523 MB）：sha256 全量 2.93 s，860 MB/s，单件 6.6 ms。
所以一级判重不需要任何缓存优化，直接算即可。
"""
from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

CHUNK = 1 << 20


def sha256_file(path: Path | str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def doc_hash(path: Path | str) -> str:
    """主键用的短指纹：sha256 前 16 位（碰撞概率在 1e5 量级文件下可忽略）。"""
    return sha256_file(path)[:16]


def normalize_text(text: str) -> str:
    """归一：全半角、去空白、去标点，只留文字，用于跨格式的内容比对。"""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    return text.lower()


def char_fingerprint(text: str, k: int = 300) -> list[int]:
    """字符指纹：归一后取 5-gram，保留最小的 k 个 64 位哈希（minhash 近似）。

    比对用 similarity()，用于"改名 / 重新导出 / 格式转换"的同源判定。
    """
    norm = normalize_text(text)
    if len(norm) < 5:
        return []
    hashes: list[int] = []
    for i in range(len(norm) - 4):
        gram = norm[i:i + 5]
        h = int.from_bytes(hashlib.blake2b(gram.encode(), digest_size=8).digest(), "big")
        hashes.append(h)
    return sorted(set(hashes))[:k]


def similarity(a: list[int], b: list[int]) -> float:
    """两枚字符指纹的相似度（交集 / 较小集合大小）。"""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    return len(sa & sb) / min(len(sa), len(sb))


def pixel_fingerprint(pdf_path: Path | str, page: int = 0, dpi: int = 72) -> str | None:
    """PDF 首页渲染后的像素指纹：抓"同一版 PDF 被改名重新导出"。

    只对疑似候选调用（同名 / 尺寸接近），不参与主流程。
    """
    try:
        import pymupdf
    except ImportError:
        return None
    try:
        with pymupdf.open(str(pdf_path)) as doc:
            if doc.page_count == 0:
                return None
            pix = doc[page].get_pixmap(dpi=dpi)
            return hashlib.md5(pix.samples).hexdigest()
    except Exception:
        return None
