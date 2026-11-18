# -*- coding: utf-8 -*-
"""note 结构化（T12）：`document_events` 事件表 + 旧 note 子串兼容层。

背景：`converted_to:` / `replaced_by:` / `needs_convert` / `moved_from:` 原先全靠
"note 里找子串"，note 只增不减，一个字段被两三个工具各解析一遍；想加一个事件就得改
所有解析方，且子串之间会互相污染（note 里出现一个文件名就叫 `converted_to:x` 的
记录就误判）。

落点：
  - 新事件写 `document_events(doc_hash, kind, payload, at)`，payload 是规范化 JSON
    （sort_keys + 无空格），`UNIQUE(doc_hash, kind, payload)` 保证重复写幂等；
  - 写 note 的工具**同时**写事件（note 继续保留人类可读留痕，不删不降级）；
  - 读方（purge / verify G5）先查事件，查不到再退回 `parse_note()`——旧库没跑迁移
    时 `document_events` 表不存在，`available()` 会返回 False，读方自动走兼容路径。

旧行迁移：`backfill()` 把历史 note 逐条解析成事件（迁移 011 里自动跑一次，
需要重跑时 `python tools/events_backfill.py`）。
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

KIND_CONVERTED_TO = "converted_to"
KIND_REPLACED_BY = "replaced_by"
KIND_MOVED_FROM = "moved_from"
KIND_RENAMED_FROM = "renamed_from"
KIND_REVIVED_FROM = "revived_from"
KIND_RESURRECTED_FROM = "resurrected_from"
KIND_TO_QUARANTINE_FROM = "to_quarantine_from"
KIND_NEEDS_CONVERT = "needs_convert"
KIND_REVIEW = "review_cli"
KIND_UNKNOWN = "note_fragment"     # 解析不出来的历史片段，原样留档

# note 里"前缀:值"形式的事件
_PREFIX_KINDS = (
    KIND_CONVERTED_TO, KIND_REPLACED_BY, KIND_MOVED_FROM, KIND_RENAMED_FROM,
    KIND_REVIVED_FROM, KIND_RESURRECTED_FROM, KIND_TO_QUARANTINE_FROM,
)
_HASH_RE = re.compile(r"^[0-9a-f]{8,64}$")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def dumps(payload: dict | None) -> str:
    """规范化 JSON：同样的语义只有一种字节表示，UNIQUE 约束才真的能去重。"""
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(payload: str) -> dict:
    try:
        return json.loads(payload or "{}")
    except json.JSONDecodeError:
        return {}


def available(conn: sqlite3.Connection) -> bool:
    """表在不在（只读连接、老库没跑迁移时为 False）。"""
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='document_events'"
        ).fetchone() is not None
    except sqlite3.OperationalError:
        return False


def append(conn: sqlite3.Connection, doc_hash: str, kind: str, payload: dict | None = None,
           at: str | None = None) -> bool:
    """追加一条事件；同 (doc_hash, kind, payload) 重复写返回 False（幂等）。"""
    if not available(conn):
        return False
    cur = conn.execute(
        "INSERT OR IGNORE INTO document_events (doc_hash, kind, payload, at) VALUES (?,?,?,?)",
        (doc_hash, kind, dumps(payload), at or now()),
    )
    return bool(cur.rowcount)


def rows(conn: sqlite3.Connection, doc_hash: str, kind: str | None = None) -> list[dict]:
    if not available(conn):
        return []
    sql = "SELECT kind, payload, at FROM document_events WHERE doc_hash=?"
    args: list = [doc_hash]
    if kind:
        sql += " AND kind=?"
        args.append(kind)
    sql += " ORDER BY id"
    return [{"kind": r["kind"], "payload": loads(r["payload"]), "at": r["at"]}
            for r in conn.execute(sql, args).fetchall()]


def has(conn: sqlite3.Connection, doc_hash: str, kind: str) -> bool:
    if not available(conn):
        return False
    return conn.execute(
        "SELECT 1 FROM document_events WHERE doc_hash=? AND kind=? LIMIT 1", (doc_hash, kind)
    ).fetchone() is not None


def _split_target(body: str) -> dict:
    """`<dir>/<name>#<hash>` / `<dir>/<name>` / `<name>` → {dir, name, hash}。"""
    body = body.strip().replace("\\", "/")
    target, digest = body, None
    if "#" in body:
        left, right = body.rsplit("#", 1)
        if _HASH_RE.match(right.strip()):
            target, digest = left, right.strip()
    name = target.rsplit("/", 1)[-1]
    dir_path = target.rsplit("/", 1)[0] if "/" in target else None
    return {"dir": dir_path, "name": name, "hash": digest}


def parse_note(note: str | None) -> list[tuple[str, dict]]:
    """旧 note → [(kind, payload)]。解析不出的片段记为 `note_fragment`（原样留档）。"""
    out: list[tuple[str, dict]] = []
    for part in (note or "").split(" | "):
        p = part.strip()
        if not p:
            continue
        if p.startswith("["):
            out.append((KIND_REVIEW, {"raw": p}))
            continue
        prefix = next((k for k in _PREFIX_KINDS if p.startswith(k + ":")), None)
        if prefix is not None:
            body = p[len(prefix) + 1:]
            if prefix == KIND_CONVERTED_TO:
                out.append((prefix, _split_target(body)))
            elif prefix == KIND_REPLACED_BY:
                out.append((prefix, {"hash": body.strip()}))
            else:
                out.append((prefix, {"rel": body.strip()}))
            continue
        if p == KIND_NEEDS_CONVERT or p.startswith(KIND_NEEDS_CONVERT + ":"):
            out.append((KIND_NEEDS_CONVERT, {}))
            continue
        out.append((KIND_UNKNOWN, {"text": p}))
    return out


def converted_targets(conn: sqlite3.Connection, doc_hash: str, note: str | None) -> list[dict]:
    """该记录的所有 converted_to 目标：事件优先，没有事件才解析 note（去重保序）。"""
    ev = [r["payload"] for r in rows(conn, doc_hash, KIND_CONVERTED_TO)]
    src = ev or [pl for k, pl in parse_note(note) if k == KIND_CONVERTED_TO]
    seen, out = set(), []
    for t in src:
        key = (t.get("dir"), t.get("name"), t.get("hash"))
        if t.get("name") and key not in seen:
            seen.add(key)
            out.append(t)
    return out


def backfill(conn: sqlite3.Connection, *, limit: int = 0) -> dict:
    """把历史 note 解析成事件（迁移 011 / 手工重跑共用）；幂等，可反复跑。"""
    if not available(conn):
        return {"rows": 0, "events_added": 0, "skipped": "document_events 表不存在"}
    sql = "SELECT doc_hash, note FROM source_documents WHERE note IS NOT NULL AND note != ''"
    args: tuple = ()
    if limit:
        sql += " LIMIT ?"
        args = (limit,)
    n_rows = n_new = 0
    for r in conn.execute(sql, args).fetchall():
        n_rows += 1
        for kind, payload in parse_note(r["note"]):
            if append(conn, r["doc_hash"], kind, payload):
                n_new += 1
    return {"rows": n_rows, "events_added": n_new}
