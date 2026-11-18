# -*- coding: utf-8 -*-
"""classify SQL 构造安全测试（2026-11-18 审核 P1：--status/--limit 注入面）。

根因：main() 把命令行 --status 直接 f-string 拼进 SQL，传
`classified' OR '1'='1` 可使引号逃逸（实证：2 行表返回 2 行 vs 正常 1 行）。
修复目标：提取 build_select_sql(status, redo, limit) 纯函数——
  ① status 走 schema CHECK 白名单，非法值抛 ValueError；
  ② status 值参数化（? 占位），不拼进 SQL 文本；
  ③ limit 强转非负整数，非法抛 ValueError、0 表示无 LIMIT。
"""
import pytest

from tools import classify


def test_inject_status_rejected():
    evil = "classified' OR '1'='1"
    with pytest.raises(ValueError, match="非法状态"):
        classify.build_select_sql(evil, False, 0)


def test_valid_status_uses_placeholder():
    sql, params = classify.build_select_sql("classified", False, 0)
    assert "?" in sql
    assert "classified" not in sql          # 值不出现在 SQL 文本里
    assert params == ["classified"]


def test_redo_ignores_status_and_no_params():
    sql, params = classify.build_select_sql("whatever-even-evil", True, 0)
    assert "NOT IN" in sql
    assert "?" not in sql
    assert params == []


def test_limit_is_integer_and_clamped():
    sql, _ = classify.build_select_sql("classified", False, 10)
    assert "LIMIT 10" in sql
    with pytest.raises(ValueError):
        classify.build_select_sql("classified", False, -1)
    with pytest.raises(ValueError):
        classify.build_select_sql("classified", False, "10 OR 1=1")


def test_all_schema_statuses_accepted():
    allowed = ["pending_classify", "classified", "processing", "processed",
               "rejected", "failed", "duplicate", "moved", "renamed",
               "absent", "superseded", "quarantined"]
    for s in allowed:
        sql, params = classify.build_select_sql(s, False, 0)
        assert params == [s]
