# -*- coding: utf-8 -*-
"""标签配置表（T24）：一级/二级标签的运行时事实源。

- 存储：`label_taxonomy(level1, level2, synonyms, enabled, sort)`（迁移 013 建表并灌预设）；
- 预设：`meta/tag_vocab.yaml`（一级 → 二级）+ `meta/tag_synonyms.yaml`（别名 → 二级）；
- 读取：`load(conn)` 查表；表空/表缺失 → 回退 YAML（新库首连、测试夹具不依赖迁移也能跑）；
- 管理：`python -m mianjing_kb.taxonomy --list/--add/--disable/--export/--seed`。

与 labelspec 的分工：labelspec 定义"维度与上限"，taxonomy 定义"标签取值集合"；
batch_label 的 prompt 用 taxonomy 的一二级分组，归一用 taxonomy 的 aliases。
"""
from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mianjing_kb.kb import config, db, wlock  # noqa: E402

DEFAULT_GROUPS = {
    "Java": ["JVM", "垃圾回收", "类加载", "集合", "并发", "线程池", "锁", "Spring"],
    "数据库": ["索引", "事务", "InnoDB", "MVCC", "锁机制", "分库分表"],
    "缓存与中间件": ["Redis", "缓存", "持久化", "集群", "过期策略", "分布式锁", "消息队列"],
    "计算机网络": ["TCP", "UDP", "HTTP", "HTTPS", "DNS"],
    "操作系统": ["进程与线程", "内存管理", "死锁", "IO模型"],
    "前端": ["React", "Vue", "TypeScript", "浏览器渲染", "前端工程化"],
    "AI与大模型": ["Transformer", "LoRA", "RAG", "微调", "量化", "强化学习"],
    "算法与数据结构": ["二叉树", "动态规划", "排序", "算法复杂度", "手撕代码"],
    "项目与行为": ["项目追问", "难点与取舍", "职业规划"],
    "工程与质量": ["幂等", "限流", "熔断", "监控", "数据一致"],
    "产品与运营": ["产品设计", "需求分析", "用户增长", "数据运营", "运营策略"],
    "求职与行为": ["自我介绍", "求职动机", "薪资谈判", "面试复盘"],
}


def _yaml_groups() -> dict[str, list[str]]:
    try:
        import yaml
        path = Path(config.TAG_VOCAB_FILE)
        if not path.exists():
            return dict(DEFAULT_GROUPS)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        groups = data.get("groups") or {}
        out = {str(k): [str(x).strip() for x in (v or []) if str(x).strip()]
               for k, v in groups.items()}
        return out or dict(DEFAULT_GROUPS)
    except Exception:  # noqa: BLE001
        return dict(DEFAULT_GROUPS)


def _yaml_synonyms() -> dict[str, str]:
    try:
        import yaml
        path = Path(config.TAG_SYNONYMS_FILE)
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(k).strip().lower(): str(v).strip() for k, v in data.items()
                if str(k).strip() and str(v).strip()}
    except Exception:  # noqa: BLE001
        return {}


def _groups_from_rows(rows) -> list[dict]:
    groups: dict[str, list[str]] = {}
    for r in rows:
        groups.setdefault(r["level1"], []).append(r["level2"])
    return [{"level1": k, "level2": v} for k, v in groups.items()]


@lru_cache(maxsize=1)
def _yaml_snapshot() -> dict:
    """YAML 快照：groups / aliases / tags（供无库路径与 prompt 使用）。"""
    groups = _yaml_groups()
    syn = _yaml_synonyms()
    tags = [t for g in groups.values() for t in g]
    aliases = {}
    for alias, target in syn.items():
        if target in tags:
            aliases[alias] = target
    return {"groups": [{"level1": k, "level2": v} for k, v in groups.items()],
            "aliases": aliases, "tags": tags, "source": "yaml"}


def load(conn=None) -> dict:
    """读配置表；conn 为 None / 表为空 / 表缺失 → 回退 YAML 快照。"""
    if conn is None:
        return _yaml_snapshot()
    try:
        rows = conn.execute(
            "SELECT level1, level2, synonyms FROM label_taxonomy "
            "WHERE enabled=1 ORDER BY level1, sort, level2").fetchall()
    except Exception:  # noqa: BLE001 —— 表未建（旧库未迁移）走 YAML
        return _yaml_snapshot()
    if not rows:
        return _yaml_snapshot()
    groups = _groups_from_rows(rows)
    tags = [r["level2"] for r in rows]
    aliases: dict[str, str] = {}
    for r in rows:
        try:
            for a in json.loads(r["synonyms"] or "[]"):
                if str(a).strip():
                    aliases[str(a).strip().lower()] = r["level2"]
        except (TypeError, ValueError):
            continue
    return {"groups": groups, "aliases": aliases, "tags": tags, "source": "table"}


def seed(conn, *, only_if_empty: bool = True) -> int:
    """把 YAML 预设灌进配置表；已存在的 (level1, level2) 不覆盖。返回新增条数。"""
    if only_if_empty and conn.execute("SELECT COUNT(*) FROM label_taxonomy").fetchone()[0]:
        return 0
    snap = _yaml_snapshot()
    syn_map = _yaml_synonyms()
    added = 0
    for gi, g in enumerate(snap["groups"]):
        for ti, level2 in enumerate(g["level2"]):
            syn = [a for a, t in syn_map.items() if t == level2]
            cur = conn.execute(
                "INSERT OR IGNORE INTO label_taxonomy (level1, level2, synonyms, enabled, sort) "
                "VALUES (?,?,?,1,?)", (g["level1"], level2, json.dumps(sorted(syn), ensure_ascii=False),
                                      gi * 100 + ti))
            added += cur.rowcount
    conn.commit()          # 不提交则 CLI 退出即回滚，--seed 变空操作
    return added


def add(conn, level1: str, level2: str, synonyms: list[str] | None = None) -> str:
    level1, level2 = level1.strip(), level2.strip()
    if not level1 or not level2:
        raise ValueError("level1/level2 不能为空")
    cur = conn.execute(
        "INSERT OR IGNORE INTO label_taxonomy (level1, level2, synonyms, enabled, sort) "
        "VALUES (?,?,?,1,?)",
        (level1, level2, json.dumps(sorted(synonyms or []), ensure_ascii=False), 9999))
    conn.commit()
    return "added" if cur.rowcount else "exists"


def disable(conn, spec: str) -> int:
    """spec = 'level2' 或 'level1/level2'；返回停用行数。"""
    if "/" in spec:
        l1, l2 = spec.split("/", 1)
        cur = conn.execute("UPDATE label_taxonomy SET enabled=0 WHERE level1=? AND level2=?",
                           (l1.strip(), l2.strip()))
    else:
        cur = conn.execute("UPDATE label_taxonomy SET enabled=0 WHERE level2=?", (spec.strip(),))
    conn.commit()
    return cur.rowcount


def export_yaml(conn, path: Path) -> Path:
    import yaml
    data = {"groups": {g["level1"]: g["level2"] for g in load(conn)["groups"]}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def prompt_lines(tax: dict | None = None, max_groups: int = 14, max_tags: int = 170) -> str:
    """prompt 用的一二级分组摘要：`Java（JVM、集合…）；数据库（索引…）`。

    预算按当前词表规模（12 组 / 131 项，2026-11-23 P1 升格后）留余量；新增分组后不要回落到截断。
    """
    tax = tax or _yaml_snapshot()
    parts = []
    budget = max_tags
    for g in tax["groups"][:max_groups]:
        if budget <= 0:
            break
        items = g["level2"][:budget]
        budget -= len(items)
        parts.append(f"{g['level1']}（{'、'.join(items)}）")
    return "；".join(parts)


def _run_cli(args) -> int:
    conn = db.connect(args.db, create=False)
    try:
        if args.seed:
            print(json.dumps({"seeded": seed(conn, only_if_empty=False)}, ensure_ascii=False))
        if args.add:
            if "/" not in args.add:
                print("[fatal] --add 需要 一级/二级 格式", file=sys.stderr)
                return 2
            l1, l2 = args.add.split("/", 1)
            print(json.dumps({"add": f"{l1}/{l2}", "result": add(conn, l1, l2, args.synonym)},
                             ensure_ascii=False))
        if args.disable:
            print(json.dumps({"disabled": disable(conn, args.disable)}, ensure_ascii=False))
        if args.export:
            print(json.dumps({"exported": str(export_yaml(conn, Path(args.export)))},
                             ensure_ascii=False))
        if args.list or (not args.add and not args.disable and not args.export):
            tax = load(conn)
            if args.json:
                print(json.dumps(tax, ensure_ascii=False, indent=1))
            else:
                for g in tax["groups"]:
                    print(f"{g['level1']}：{'、'.join(g['level2'])}")
                print(f"[source] {tax['source']}  tags={len(tax['tags'])} aliases={len(tax['aliases'])}")
    finally:
        conn.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="标签配置表管理（一级/二级预设）")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--list", action="store_true", help="列出一二级标签（表为空/缺表回退 YAML）")
    ap.add_argument("--json", action="store_true", help="--list 输出 JSON")
    ap.add_argument("--add", default="", metavar="一级/二级", help='新增，如 "Java/Netty"')
    ap.add_argument("--synonym", action="append", default=[], help="配合 --add：别名（可多次）")
    ap.add_argument("--disable", default="", metavar="二级或一级/二级", help="停用标签")
    ap.add_argument("--export", default="", help="导出为 YAML")
    ap.add_argument("--seed", action="store_true", help="从 meta/tag_vocab.yaml 补灌缺失项")
    args = ap.parse_args(argv)
    if not any((args.list, args.add, args.disable, args.export, args.seed)):
        ap.print_help()
        return 2
    mutating = bool(args.seed or args.add or args.disable)
    try:
        if mutating:
            with wlock.write_lock("kb", tool="taxonomy", build_dir=Path(args.db).parent,
                                  target=args.db):
                return _run_cli(args)
        return _run_cli(args)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex)}, ensure_ascii=False, indent=1),
              file=sys.stderr)
        return 2




if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
