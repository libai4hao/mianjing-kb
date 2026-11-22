# -*- coding: utf-8 -*-
"""T31 `kb status`：只读汇总当前流水线状态，回答"现在卡在哪、门禁是否过期"。

不重跑任何阶段、不加写锁、不建库。数据源：
  - kb.sqlite：source_documents / questions / document_products / labels_staging /
    golden_labels / batches
  - 面经库.db：questions / vec_questions / question_tags（存在才查）
  - build 产物：verify_report.json、cluster_result.json、refresh_log.jsonl
  - locks：build/locks/kb.lock、online.lock

用法：
    kb status
    kb status --json
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, status as st, wlock  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
STALE_SEC = 24 * 3600          # 门禁报告超过一天且库已变，标 STALE


def _ro_uri(path: Path) -> str:
    return Path(path).resolve().as_uri() + "?mode=ro"


def _open_ro(path: Path) -> sqlite3.Connection | None:
    if not path.exists():
        return None
    try:
        con = sqlite3.connect(_ro_uri(path), uri=True)
        con.row_factory = sqlite3.Row
        return con
    except sqlite3.Error:
        return None


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()[0] > 0


def _column_exists(con, table: str, column: str) -> bool:
    """老库可能还没跑 018 迁移：列不存在时不要拿 answer_state 去查。"""
    return any(r[1] == column for r in con.execute(f"PRAGMA table_info({table})"))


def _scalar(con, sql: str, default: int | None = 0) -> int | None:
    try:
        row = con.execute(sql).fetchone()
        return int(row[0]) if row and row[0] is not None else default
    except sqlite3.Error:
        return default


def _identity(path: Path) -> dict:
    try:
        s = path.stat()
        return {"path": str(path), "size_bytes": s.st_size, "mtime_ns": s.st_mtime_ns}
    except OSError:
        return {"path": str(path), "missing": True}


def _load_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _kb_summary(con) -> dict:
    """事实源：源状态 / 产物 / 抽题 / 打标 / 批次。"""
    out: dict = {"present": True}
    by_status = {r["status"]: int(r["n"]) for r in con.execute(
        "SELECT status, COUNT(*) AS n FROM source_documents GROUP BY status ORDER BY n DESC")}
    out["source"] = {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "ingested": sum(by_status.get(s, 0) for s in st.INGESTED),
        "pending_classify": by_status.get(st.PENDING, 0),
        "rejected": by_status.get(st.REJECTED, 0),
        "superseded": by_status.get(st.SUPERSEDED, 0),
    }

    n_products = _scalar(con, "SELECT COUNT(*) FROM document_products", None)
    if n_products is None:
        out["products"] = {"present": False}
    else:
        out["products"] = {
            "present": True,
            "total": n_products,
            "not_extracted": _scalar(
                con, "SELECT COUNT(*) FROM document_products WHERE extracted_at IS NULL"),
            "zero_questions": _scalar(
                con, "SELECT COUNT(*) FROM document_products WHERE questions_n=0"),
            "not_indexed": _scalar(
                con, "SELECT COUNT(*) FROM document_products WHERE indexed_at IS NULL"),
            "by_text_source": {r["text_source"]: int(r["n"]) for r in con.execute(
                "SELECT text_source, COUNT(*) AS n FROM document_products "
                "GROUP BY text_source ORDER BY n DESC")},
        }

    out["pipeline"] = {
        # 待产物的源：在内容池里但还没有 document_products 行
        "pending_product": _scalar(
            con,
            f"SELECT COUNT(*) FROM source_documents sd WHERE sd.status IN {st.sql_in(st.INGESTED)} "
            "AND NOT EXISTS (SELECT 1 FROM document_products dp WHERE dp.doc_hash=sd.doc_hash)"),
        # 待抽题 = 有产物行但还没抽过（extracted_at 空）；0 题算已处理
        "pending_extract": _scalar(
            con, "SELECT COUNT(*) FROM document_products WHERE extracted_at IS NULL"),
    }

    if _table_exists(con, "questions"):
        # 有效主题：题级 topic 为空时继承文档 topic（G3 同款口径），否则会把 20k 题全算待打标。
        eff = ("COALESCE(NULLIF(q.topic,''), NULLIF(sd.topic,''), '')")
        out["questions"] = {
            "total": _scalar(con, "SELECT COUNT(*) FROM questions"),
            "with_topic": _scalar(
                con, "SELECT COUNT(*) FROM questions q LEFT JOIN source_documents sd "
                     f"ON sd.doc_hash=q.doc_hash WHERE {eff} NOT IN ('', '{config.UNCLASSIFIED}')"),
            "missing_topic": _scalar(
                con, "SELECT COUNT(*) FROM questions q LEFT JOIN source_documents sd "
                     f"ON sd.doc_hash=q.doc_hash WHERE {eff} IN ('', '{config.UNCLASSIFIED}')"),
            "with_difficulty": _scalar(
                con, "SELECT COUNT(*) FROM questions WHERE difficulty IS NOT NULL"),
            "with_tags": _scalar(
                con, "SELECT COUNT(*) FROM questions WHERE COALESCE(tags,'') != ''"),
            "with_q_key": _scalar(
                con, "SELECT COUNT(*) FROM questions WHERE COALESCE(q_key,'') != ''"),
        }
        if _column_exists(con, "questions", "answer_state"):
            out["questions"]["by_answer_state"] = {
                r[0] or "unmarked": r[1] for r in con.execute(
                    "SELECT answer_state, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC")
            }
    else:
        out["questions"] = {"present": False}

    if _table_exists(con, "labels_staging"):
        out["labels"] = {
            "staging": _scalar(con, "SELECT COUNT(*) FROM labels_staging"),
            "review_pending": _scalar(
                con, "SELECT COUNT(*) FROM labels_staging WHERE review_state='pending'"),
            "golden": _scalar(con, "SELECT COUNT(*) FROM golden_labels", None),
        }
    else:
        out["labels"] = {"staging": 0, "review_pending": 0, "golden": 0}

    if _table_exists(con, "batches"):
        out["batches"] = {
            "running": _scalar(con, "SELECT COUNT(*) FROM batches WHERE state='running'"),
            "failed": _scalar(con, "SELECT COUNT(*) FROM batches WHERE state='failed'"),
            "latest": [{"batch_id": r["batch_id"], "state": r["state"],
                        "created_at": r["created_at"], "updated_at": r["updated_at"]}
                       for r in con.execute(
                           "SELECT batch_id, state, created_at, updated_at FROM batches "
                           "ORDER BY COALESCE(updated_at, created_at) DESC LIMIT 3")],
        }
    return out


def _retrieval_summary(path: Path) -> dict:
    con = _open_ro(path)
    if con is None:
        return {"present": False, "path": str(path)}
    try:
        # vec0 是虚拟表：只读连接也要加载扩展，否则 COUNT 会报 no such module。
        try:
            import sqlite_vec
            con.enable_load_extension(True)
            sqlite_vec.load(con)
            con.enable_load_extension(False)
        except Exception:  # noqa: BLE001
            pass
        out: dict = {"present": True, "path": str(path)}
        out["questions"] = _scalar(con, "SELECT COUNT(*) FROM questions", None) \
            if _table_exists(con, "questions") else None
        out["vec_questions"] = _scalar(con, "SELECT COUNT(*) FROM vec_questions", None) \
            if _table_exists(con, "vec_questions") else None
        out["question_tags"] = _scalar(con, "SELECT COUNT(*) FROM question_tags", None) \
            if _table_exists(con, "question_tags") else None
        if _table_exists(con, "vec_meta"):
            out["vec_meta"] = {r["key"]: r["value"] for r in con.execute(
                "SELECT key, value FROM vec_meta")}
        return out
    finally:
        con.close()


def _gate_summary(build_dir: Path, kb_id: dict, out_id: dict) -> dict:
    report = _load_json(build_dir / "verify_report.json")
    if not report:
        return {"present": False, "state": "MISSING",
                "note": "没有 verify_report.json；跑 kb verify"}
    summary = report.get("summary") or {}
    bound = summary.get("db_identity") or {}
    stale = False
    reasons = []
    for name, cur in (("kb", kb_id), ("out", out_id)):
        old = bound.get(name) or {}
        if not old:
            stale = True
            reasons.append(f"{name}:报告未绑定指纹")
        elif old.get("size_bytes") != cur.get("size_bytes") or \
                old.get("mtime_ns") != cur.get("mtime_ns"):
            stale = True
            reasons.append(f"{name}:库已变化")
    age = None
    try:
        age = int(time.time() - (build_dir / "verify_report.json").stat().st_mtime)
    except OSError:
        pass
    state = "STALE" if stale else ("FAIL" if summary.get("verdict") == "FAIL" else "PASS")
    if not stale and age is not None and age > STALE_SEC:
        # 文件旧但库没变仍是有效结论；单独标 age 供人判断，不升级为 STALE
        pass
    # 过期报告的 verdict 不作数：库里已变，PASS 是对旧库的结论（P0-2，docs/47）
    effective = "INVALID" if stale else summary.get("verdict")
    return {"present": True, "state": state, "verdict": summary.get("verdict"),
            "effective_verdict": effective,
            "at": summary.get("at"), "age_sec": age, "stale": stale,
            "stale_reasons": reasons, "gates": summary.get("gates"),
            "hard_fail": summary.get("hard_fail")}


def _cluster_summary(build_dir: Path) -> dict:
    data = _load_json(build_dir / "cluster_result.json")
    if not data:
        return {"present": False}
    return {"present": True,
            "complete": data.get("complete"),
            "pool_docs": data.get("stats", {}).get("docs"),
            "questions_99": data.get("stats", {}).get("q99_questions"),
            "verdict99": data.get("verdict99"),
            "elapsed_sec": data.get("elapsed_sec")}


def _lock_summary(build_dir: Path) -> dict:
    """只报告实际阻塞写入的锁；advisory lock 文件可永久存在，不代表锁仍活跃。

    refresh.lock 使用 O_EXCL 文件存在性作为互斥协议，因此即使进程崩溃留下残件，
    它仍会阻塞下一次 refresh，必须继续展示为 BLOCKING，不能按 advisory lock 探测。
    """
    out = {}
    for name in ("kb.lock", "online.lock", "refresh.lock"):
        p = build_dir / "locks" / name if name != "refresh.lock" else build_dir / name
        if not p.exists():
            continue

        if name == "refresh.lock":
            locked, holder = True, None  # O_EXCL：残留文件本身仍阻止 refresh 启动
        else:
            locked, holder = wlock.inspect_lock(p)
            if locked is False:
                continue

        item: dict = {"path": str(p), "age_sec": None}
        if name == "refresh.lock":
            item["state"] = "BLOCKING"  # 活跃进程或崩溃残件，二者都会阻止 refresh
        else:
            item["state"] = "ACTIVE" if locked is True else "UNKNOWN"
        try:
            item["age_sec"] = int(time.time() - p.stat().st_mtime)
        except OSError:
            pass

        if name == "refresh.lock":
            try:
                item["raw"] = p.read_text(encoding="utf-8").strip()
            except OSError:
                item["raw"] = None
        else:
            item.update(holder or {})
        out[name] = item
    return out


def collect() -> dict:
    build = Path(config.BUILD_DIR)
    kb_path = Path(config.DB_PATH)
    online = build / "面经库.db"
    kb_con = _open_ro(kb_path)
    try:
        kb = _kb_summary(kb_con) if kb_con is not None else {"present": False}
    finally:
        if kb_con is not None:
            kb_con.close()
    kb_id, out_id = _identity(kb_path), _identity(online)
    gates = _gate_summary(build, kb_id, out_id)
    attention = []
    if not kb.get("present"):
        attention.append("kb.sqlite 不存在")
    if gates["state"] in ("STALE", "FAIL", "MISSING"):
        attention.append(f"门禁 {gates['state']}")
    if not gates.get("present") or gates.get("stale"):
        attention.append("门禁报告需重跑")
    labels = kb.get("labels") or {}
    if labels.get("review_pending"):
        attention.append(f"打标待复核 {labels['review_pending']} 条")
    batches = kb.get("batches") or {}
    if batches.get("failed"):
        attention.append(f"失败批次 {batches['failed']} 个")
    if batches.get("running"):
        attention.append(f"运行中批次 {batches['running']} 个")
    locks = _lock_summary(build)
    if locks:
        attention.append("存在写锁：" + "、".join(locks))
    return {
        "generated_at": datetime.now().strftime(NOW_FMT),
        "paths": {"corpus": str(config.CORPUS_ROOT), "build": str(build),
                  "kb": str(kb_path), "retrieval": str(online)},
        "kb": kb,
        "retrieval": _retrieval_summary(online),
        "gates": gates,
        "cluster": _cluster_summary(build),
        "locks": locks,
        "attention": attention,
    }


def _fmt(n) -> str:
    return "-" if n is None else str(n)


def _gate_line(gates: dict) -> str:
    """门禁一行：过期报告的原 verdict 不作数（P0-2，docs/47）。"""
    state = gates.get("state")
    verdict = gates.get("effective_verdict") or gates.get("verdict")
    if gates.get("stale") and gates.get("verdict"):
        verdict = f"INVALID（报告 {gates['verdict']} 已过期）"
    return f"  {state}  verdict={verdict}  at={gates.get('at')}"


def render_text(data: dict) -> str:
    kb = data["kb"]
    src = kb.get("source") or {}
    prod = kb.get("products") or {}
    pipe = kb.get("pipeline") or {}
    qs = kb.get("questions") or {}
    labels = kb.get("labels") or {}
    ret = data["retrieval"]
    gates = data["gates"]
    lines = [
        f"mianjing-kb status  {data['generated_at']}",
        f"语料根  {data['paths']['corpus']}",
        "",
        "[源]",
        f"  总数 {_fmt(src.get('total'))}  内容池 {_fmt(src.get('ingested'))}  "
        f"待分类 {_fmt(src.get('pending_classify'))}  退役 {_fmt(src.get('superseded'))}  "
        f"拒绝 {_fmt(src.get('rejected'))}",
        "  状态 " + json.dumps(src.get("by_status") or {}, ensure_ascii=False),
        "",
        "[处理]",
        f"  产物 {_fmt(prod.get('total'))}  未抽题 {_fmt(pipe.get('pending_extract'))}  "
        f"0 题 {_fmt(prod.get('zero_questions'))}  未入索引 {_fmt(prod.get('not_indexed'))}",
        f"  待产物 {_fmt(pipe.get('pending_product'))}  "
        f"产物类型 {json.dumps(prod.get('by_text_source') or {}, ensure_ascii=False)}",
        "",
        "[题]",
        f"  总题 {_fmt(qs.get('total'))}  有主题 {_fmt(qs.get('with_topic'))}  "
        f"待打标 {_fmt(qs.get('missing_topic'))}  有难度 {_fmt(qs.get('with_difficulty'))}",
        f"  有标签 {_fmt(qs.get('with_tags'))}  有稳定键 {_fmt(qs.get('with_q_key'))}",
        "",
        "[打标]",
        f"  staging {_fmt(labels.get('staging'))}  待复核 {_fmt(labels.get('review_pending'))}  "
        f"金标 {_fmt(labels.get('golden'))}",
        "",
        "[检索库]",
        f"  题 {_fmt(ret.get('questions'))}  向量 {_fmt(ret.get('vec_questions'))}  "
        f"标签行 {_fmt(ret.get('question_tags'))}",
        "",
        "[门禁]",
        _gate_line(gates),
    ]
    astate = qs.get("by_answer_state") or {}
    if astate:
        i = next(i for i, x in enumerate(lines) if x == "[打标]")
        lines.insert(i - 1 if lines[i - 1] == "" else i,
                     "  答案来源 " + "  ".join(f"{k} {_fmt(v)}" for k, v in astate.items()))
    if gates.get("stale_reasons"):
        lines.append("  过期原因：" + "；".join(gates["stale_reasons"]))
    if gates.get("gates"):
        lines.append("  " + json.dumps(gates["gates"], ensure_ascii=False))
    clusters = data.get("cluster") or {}
    if clusters.get("present"):
        lines += ["", "[99 区]",
                  f"  complete={clusters.get('complete')} 池 {_fmt(clusters.get('pool_docs'))} "
                  f"题 {_fmt(clusters.get('questions_99'))} "
                  f"verdict={json.dumps(clusters.get('verdict99') or {}, ensure_ascii=False)}"]
    if data["locks"]:
        lines += ["", "[锁]", "  " + json.dumps(data["locks"], ensure_ascii=False)]
    if data["attention"]:
        lines += ["", "[需要关注]", *[f"  - {x}" for x in data["attention"]]]
    return "\n".join(lines)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="只读汇总：源/产物/题/打标/门禁/锁")
    ap.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    args = ap.parse_args(argv)
    data = collect()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(render_text(data))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
