# -*- coding: utf-8 -*-
"""文本质量度量：门禁的三个指标（CJK 占比 / 乱码率 / 有效字符率）。

背景（docs/01 §3.3）：本语料里 15 篇转换失败件**都有文本层**，只是字体编码坏了，
所以"有没有文本层"不能作为判据；`ok=true` 也可能是假成功（11 篇 / 2,537 页 CJK<0.15）。
必须用文本质量门禁替代。
"""
from __future__ import annotations

import re
import unicodedata

# 汉字区段
CJK_RANGES = (
    (0x3400, 0x4DBF),    # 扩展 A
    (0x4E00, 0x9FFF),    # 基本区
    (0xF900, 0xFAFF),    # 兼容汉字
    (0x20000, 0x2FA1F),  # 扩展 B~F
)

# 常见中文标点 + ASCII 标点，算"有效字符"
_COMMON_PUNCT = set(
    "，。、；：？！“”‘’（）【】《》〈〉—…·～＋－×÷＝％＃＠＆＊"
    ",.;:?!()[]{}<>\"'`~-_/\\|*#%&+=@$^"
)

# 本语料里出现过的"乱码特征"：阿拉伯、奥里亚、以及各类不该出现在中文文档的字母/组合符号
_GARBLED_SCRIPT_PREFIXES = (
    "ARABIC", "DEVANAGARI", "BENGALI", "GURMUKHI", "GUJARATI", "ORIYA",
    "TAMIL", "TELUGU", "KANNADA", "MALAYALAM", "SINHALA", "THAI", "LAO",
    "KHMER", "MYANMAR", "TIBETAN", "ETHIOPIC", "ARMENIAN", "GEORGIAN",
    "HEBREW", "SYRIAC", "THAANA", "MONGOLIAN", "MODIFIER LETTER",
)

_WS_RE = re.compile(r"[\s\u200b-\u200d\ufeff]+")


def is_cjk(ch: str) -> bool:
    cp = ord(ch)
    for lo, hi in CJK_RANGES:
        if lo <= cp <= hi:
            return True
    return False


def is_garbled(ch: str) -> bool:
    """乱码字符：组合附加符、替换符、控制符、非中文语系的字母。"""
    cp = ord(ch)
    if cp == 0xFFFD:                    # replacement char
        return True
    if 0x00 <= cp <= 0x08 or 0x0B <= cp <= 0x1F or 0x7F <= cp <= 0x9F:
        return True                     # 控制符
    cat = unicodedata.category(ch)
    if cat in ("Mn", "Me", "Cf"):       # 组合符号（如示例里的 ̵）
        return True
    if ch.isascii():
        return False
    try:
        name = unicodedata.name(ch)
    except ValueError:
        return True                     # 没有 Unicode 名字 → 私用区 / 未定义
    if name.startswith(_GARBLED_SCRIPT_PREFIXES):
        return True
    return False


def visible_chars(text: str) -> int:
    return len(_WS_RE.sub("", text))


def cjk_count(text: str) -> int:
    return sum(1 for ch in text if is_cjk(ch))


def cjk_ratio(text: str) -> float:
    vis = _WS_RE.sub("", text)
    if not vis:
        return 0.0
    return cjk_count(vis) / len(vis)


def garbled_ratio(text: str) -> float:
    vis = _WS_RE.sub("", text)
    if not vis:
        return 0.0
    return sum(1 for ch in vis if is_garbled(ch)) / len(vis)


def valid_ratio(text: str) -> float:
    """有效字符占比：CJK、ASCII 可见字符、常见标点算有效；其余（乱码、生僻符号）不算。"""
    vis = _WS_RE.sub("", text)
    if not vis:
        return 0.0
    ok = 0
    for ch in vis:
        if is_cjk(ch) or ch.isascii() or ch in _COMMON_PUNCT:
            ok += 1
        elif unicodedata.category(ch) in ("Nd", "Nl", "Po", "Ps", "Pe", "Pd", "Pi", "Pf"):
            ok += 1
    return ok / len(vis)


def metrics(text: str) -> dict:
    vis = visible_chars(text)
    return {
        "visible_chars": vis,
        "cjk_chars": cjk_count(text),
        "cjk_ratio": round(cjk_ratio(text), 4),
        "garbled_ratio": round(garbled_ratio(text), 4),
        "valid_ratio": round(valid_ratio(text), 4),
    }


def verdict(text: str, *, cjk_min: float, garbled_max: float,
            valid_min: float, min_visible: int) -> str:
    """判定顺序：先看乱码，再看 CJK 占比，最后看内容量。"""
    m = metrics(text)
    if m["garbled_ratio"] > garbled_max:
        return "garbled"
    if m["cjk_ratio"] < cjk_min:
        return "low_cjk"
    if m["visible_chars"] < min_visible:
        return "non_content"
    if m["valid_ratio"] < valid_min:
        return "garbled"
    return "pass"


def strip_markdown_noise(text: str) -> str:
    """去掉 md 的图片、链接目标、代码围栏标记，只留可读正文，避免指标被语法稀释。"""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^```.*$", "", text, flags=re.M)
    text = re.sub(r"^\s*[#>|-]{1,}\s*", "", text, flags=re.M)
    return text
