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


def sha256_file_sized(path: Path | str) -> tuple[str, int]:
    """一次读取同时返回 (sha256, 字节数)。

    intake 之前用 stat() 取 size、再读一遍算 hash；文件在两次调用之间被改写时
    size 与 hash 会对不上。size 直接数读到的字节，消除这个竞态。
    """
    h = hashlib.sha256()
    size = 0
    with open(path, "rb") as f:
        while True:
            b = f.read(CHUNK)
            if not b:
                break
            size += len(b)
            h.update(b)
    return h.hexdigest(), size


def doc_hash(path: Path | str) -> str:
    """主键用的短指纹：sha256 前 16 位（碰撞概率在 1e5 量级文件下可忽略）。"""
    return sha256_file(path)[:16]


def normalize_text(text: str, strip_punct: bool = True) -> str:
    """归一：全半角、去空白、去标点，只留文字，用于跨格式的内容比对。

    注意：默认去标点会把 a+b / a-b / a*b 归一成同一串，代码类题目做近重复聚类时
    可能把语义不同的题判成同题。宽松检索可以照用；代码题请传 strip_punct=False。
    """
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"\s+", "", text)
    if strip_punct:
        text = re.sub(r"[^\w\u4e00-\u9fff]", "", text)
    return text.lower()


def _grams(text: str, ngram: int = 5, strip_punct: bool = True) -> list[int]:
    norm = normalize_text(text, strip_punct=strip_punct)
    if len(norm) < ngram:
        return []
    return [int.from_bytes(hashlib.blake2b(norm[i:i + ngram].encode(), digest_size=8).digest(), "big")
            for i in range(len(norm) - ngram + 1)]


THRESHOLD_BITS = 4        # 采样率 1/16
THRESHOLD_MIN_SAMPLES = 8  # 少于这点样本，估计方差过大，返回 None 让调用方走全量


def threshold_fingerprint(text: str, bits: int = THRESHOLD_BITS,
                          strip_punct: bool = True) -> frozenset[int]:
    """阈值采样草图：只保留 h < 2^(64-bits) 的 5-gram（与文档长度无关）。

    采样只由 gram 内容决定，所以同一 gram 在任何文档里要么都被采、要么都不采，
    这样交集/min(样本数) 就是 |A∩B|/min(|A|,|B|) 的无偏估计，不受长度差影响。
    """
    limit = 1 << (64 - bits)
    return frozenset(h for h in _grams(text, strip_punct=strip_punct) if h < limit)


def threshold_containment(a: frozenset[int], b: frozenset[int]) -> float | None:
    """包含度 |A∩B| / min(|A|,|B|)，长度无关；样本太少返回 None（改用全量比对）。"""
    if len(a) < THRESHOLD_MIN_SAMPLES or len(b) < THRESHOLD_MIN_SAMPLES:
        return None
    return len(a & b) / min(len(a), len(b))


def threshold_jaccard(a: frozenset[int], b: frozenset[int]) -> float | None:
    """Jaccard 估计 |A∩B| / |A∪B|，同样只由采样后的集合直接算比值。"""
    if len(a) < THRESHOLD_MIN_SAMPLES or len(b) < THRESHOLD_MIN_SAMPLES:
        return None
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)

