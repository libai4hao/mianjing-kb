# -*- coding: utf-8 -*-
"""`source_documents.status` 的唯一定义处（T13）。

为什么集中：状态字符串原先散落在 intake / purge / classify / verify / doc_convert /
dispatch / extract 各处，新增一个状态要全仓 grep 才能确认影响面；更麻烦的是分组语义
各写一份——purge 认 `classified + superseded` 为"已收入"，extract 只认 `classified`，
classify 重判排除 `quarantined/absent/superseded/processed`，漏改一处就是数据错误
（典型：新增状态被 purge 当成可删依据）。

约定：
  - 生产代码只通过本模块引用状态字面量；SQL 里用 f-string 插值 + `sql_in()` 生成
    IN 列表（`sql_in` 会校验取值必须在 `ALL` 内，防止手写错字符串）。
  - `tests/` 保留字面量。测试是"库内实际存什么"的契约层，常量改名不应悄悄改库；
    改库格式必须让测试一起红。
  - 新增状态：先改本模块 `ALL`，再改 `meta/schema/001_init.sql` 的 CHECK 枚举，
    然后 `tests/test_status.py` 的枚举一致性用例会盯着两边不许漂移。
"""
from __future__ import annotations

from typing import Iterable

# ---------- 单值 ----------
PENDING = "pending_classify"      # 已登记、待分类
CLASSIFIED = "classified"         # 已判定为可入库内容（题池唯一来源）
PROCESSING = "processing"         # 预留：解析中
PROCESSED = "processed"           # 预留：已出产物
REJECTED = "rejected"             # 非内容（学习路线/简历模板等）
FAILED = "failed"                 # 预留：处理失败
DUPLICATE = "duplicate"           # 预留：判重命中（当前判重走独立动作，不落此态）
MOVED = "moved"                   # 预留：历史移动态
RENAMED = "renamed"               # 预留：历史改名态
ABSENT = "absent"                 # 文件消失（曾删除），内容回来时可 resurrect
SUPERSEDED = "superseded"         # 有接替件（replaced_by / converted_to）
QUARANTINED = "quarantined"       # 隔离区（99-重复文件待删），永不参与处理

# ---------- 分组 ----------
# 已进入内容池（题池只认 CLASSIFIED；PROCESSING/PROCESSED 是历史/预留态）
INGESTED = (CLASSIFIED, PROCESSING, PROCESSED)
# extract / build_db / bookmarks / backfill 的题池口径
EXTRACTABLE = (CLASSIFIED,)
# 已退役：不再读盘、不再抽题、但保留行给 G5 与 purge 审计
RETIRED = (SUPERSEDED, ABSENT)
# 隔离副本：既不是来源也不是接替件
QUARANTINED_ONLY = (QUARANTINED,)
# schema 预留 / 历史动作态：当前无工具写入，仅 CHECK 枚举与兼容读出用
RESERVED = (PROCESSING, PROCESSED, FAILED, DUPLICATE, MOVED, RENAMED)
# 全量（必须与 001_init.sql 的 CHECK 枚举逐字一致）
ALL = (
    PENDING, CLASSIFIED, PROCESSING, PROCESSED, REJECTED, FAILED,
    DUPLICATE, MOVED, RENAMED, ABSENT, SUPERSEDED, QUARANTINED,
)
# G5 台账-磁盘一致要扫的两态：在盘（classified）与已退役但可能仍在盘（superseded）
G5_SCAN = (CLASSIFIED, SUPERSEDED)
# purge 认定的"内容已收入"：classified 直接算；superseded 还要有接替件（见 purge_origin）
PURGE_OK = G5_SCAN
# converted_to 的"有效后继记录"：不能是已退役态，也不能是隔离副本
SUCCESSOR_OK = tuple(v for v in ALL if v not in RETIRED + QUARANTINED_ONLY)


def sql_in(values: Iterable[str]) -> str:
    """把状态分组渲染成 SQL IN 列表：`('classified','superseded')`。

    只接受本模块登记的取值——手滑写成 `'classifed'` 会直接 ValueError，
    而不是在 SQL 里静默匹配 0 行。
    """
    items = list(values)
    for v in items:
        if v not in ALL:
            raise ValueError(f"未登记的状态值：{v!r}（请先在 kb/status.py 的 ALL 里声明）")
    return "(" + ",".join(f"'{v}'" for v in items) + ")"


def is_valid(value: str) -> bool:
    return value in ALL
