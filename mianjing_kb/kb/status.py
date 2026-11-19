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
CLASSIFIED = "classified"         # 已分类、待处理（内容池入口态）
PROCESSING = "processing"         # 处理中（extract 开始写；崩溃后由此续跑）
PROCESSED = "processed"           # 已出产物并完成抽题（含 0 题的件；增量不再重跑）
REJECTED = "rejected"             # 非内容（学习路线/简历模板等）
FAILED = "failed"                 # 预留：处理失败（当前失败留 processing，不落此态）
DUPLICATE = "duplicate"           # 预留：判重命中（当前判重走独立动作，不落此态）
MOVED = "moved"                   # 预留：历史移动态
RENAMED = "renamed"               # 预留：历史改名态
ABSENT = "absent"                 # 文件消失（曾删除），内容回来时可 resurrect
SUPERSEDED = "superseded"         # 有接替件（replaced_by / converted_to）
QUARANTINED = "quarantined"       # 隔离区（99-重复文件待删），永不参与处理

# ---------- 分组 ----------
# 已进入内容池（题池/建库/核账都按这一组；Q-3 后 processed 是真态，不再是预留）
INGESTED = (CLASSIFIED, PROCESSING, PROCESSED)
# extract / build_db / bookmarks / backfill 的题池口径
EXTRACTABLE = INGESTED
# 已退役：不再读盘、不再抽题、但保留行给 G5 与 purge 审计
RETIRED = (SUPERSEDED, ABSENT)
# 隔离副本：既不是来源也不是接替件
QUARANTINED_ONLY = (QUARANTINED,)
# schema 预留 / 历史动作态：当前无工具写入，仅 CHECK 枚举与兼容读出用
RESERVED = (FAILED, DUPLICATE, MOVED, RENAMED)
# 全量（必须与 001_init.sql 的 CHECK 枚举逐字一致）
ALL = (
    PENDING, CLASSIFIED, PROCESSING, PROCESSED, REJECTED, FAILED,
    DUPLICATE, MOVED, RENAMED, ABSENT, SUPERSEDED, QUARANTINED,
)
# G5 台账-磁盘一致要扫的态：内容池三态都在盘；superseded 可能仍在盘
G5_SCAN = (CLASSIFIED, PROCESSING, PROCESSED, SUPERSEDED)
# purge 认定的"内容已收入"：classified / processed 直接算；
# superseded 还要有接替件（见 purge_origin）
PURGE_OK = (CLASSIFIED, PROCESSED, SUPERSEDED)
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
