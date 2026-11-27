# -*- coding: utf-8 -*-
"""CSV / 表格公式中和：低信任文本进 Excel/WPS 前必须转成纯文本。

两个控制要分开：
- CSV 结构（分隔符、引号、换行）由 `csv` 模块负责，不要手写拼接；
- 公式注入（`=cmd|'...'!A1`、`=HYPERLINK(...)`）由本模块负责：以公式字符开头，
  或前导空白后以公式字符开头的单元格，首位加一个单引号，表格软件按文本处理。

为什么单引号：Excel/WPS 对以 `'` 开头的单元格按文本存储，不再当公式求值；
UTF-8 BOM、`quoting=csv.QUOTE_ALL` 都不提供这层保护。
"""
from __future__ import annotations

FORMULA_PREFIXES = ("=", "+", "-", "@")
# Excel 还会把制表/回车/换行开头的单元格视为公式续行或被截断的前缀
CONTROL_PREFIXES = ("\t", "\r", "\n")
_TRIM = " \t\r\n"


def neutralize(value) -> str:
    """把任意单元格值转成"不会被表格软件当公式执行"的字符串。"""
    if value is None:
        return ""
    text = str(value)
    if not text:
        return text
    if text.startswith(CONTROL_PREFIXES):
        return "'" + text
    if text.lstrip(_TRIM).startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def neutralize_row(row: dict) -> dict:
    """字典行整行中和（DictWriter 用）。"""
    return {k: neutralize(v) for k, v in row.items()}
