# -*- coding: utf-8 -*-
"""verify_ledger：G1-G8 核账门禁（docs/15 §2 方案，2026-11-17 审核后落地）。

与 docs/15 原方案的两处实现偏差（docs/17 §5 有记录）：
  1. 输出为 build/verify_report.json + verify_report.md——gate_report.json 已由
     gate.py --audit 占用（文本质量维度，dispatch/ocr_merge 消费），核账门禁用独立产物；
  2. 「影子库 + 原子切换」属 refresh.py（K1 编排）职责，本脚本只做校验与清单产出。

门禁七道（每道对应一次真实事故，判定规则见 docs/15 §2 表格）：
  G1 源-库全覆盖（硬）：status='classified' 每篇须「doc_contents 有行 且 有题」，
     否则须在 meta/skip_log.json 登记明确原因
     （no_text_layer / garbled / converted_only / duplicate_of / no_question）
  G2 转换链闭环（硬）：磁盘 md 未进 doc_contents → FAIL；ocr_text 有全文却无 md → FAIL；
     doc_contents 有行却 0 题且未标 no_question → FAIL
  G3 主题唯一口径（硬）：检索库 questions.topic 空串/'未分类' 计数须为 0；两种表示混用即口径问题
  G4 索引一致性（硬）：vec_questions 与 questions 行数相等且 qid(rowid) 集合相等；
     questions_fts 同数。vec 表未建 → WARN（向量层是可选层，embed_questions 未跑）
  G5 台账-磁盘一致（硬）：classified → src_path 在盘；superseded → 在 99 区或已删且留痕
     （note / build/retire_log.csv）；99 区每件都能在台账找到
  G6 打标质量（软→硬）：labels_staging 未建（线上打标未启用），报 SKIP
  G8 单身份（硬）：有源回链的 md 产物不得再有 `md-mirror:` 身份的题行（P0-1 双份检测）
  G7 文档一致性（软，--docs）：README 声称的测试数 vs pytest --collect-only 实际；
     「尚未实现」清单不得列 mianjing_kb/ 已存在的脚本；docs 编号范围 vs docs/ 实际

输出（build/）：
  verify_report.json / verify_report.md   任一硬门禁 FAIL 即非 0 退出
  backfill_plan.csv                        G1/G2 FAIL 项即补录清单（门禁即清单）

用法：
    python -m mianjing_kb.verify_ledger            # G1-G5 + G6（刷新后必跑）
    python -m mianjing_kb.verify_ledger --docs     # 加 G7（pre-commit / 文档变更后）
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, events, hashing, labelspec, status  # noqa: E402
from mianjing_kb.kb.vocab import topic_vocab  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
SKIP_REASONS = ("no_text_layer", "garbled", "converted_only", "duplicate_of",
                "no_question", "chunker_no_split", "no_md_artifact")
# G6 的 OK 判定直接决定 merge 能否继续，等级必须是硬（"软→硬"字符串永远不会进 hard_fail）
LEVEL_HARD = "硬"
LEVEL_SOFT = "软"


def norm_stem(stem: str) -> str:
    """stem 归一：去首尾空白 + 小写——台账文件名可能带尾随空格（如「面向字节_Java集合 .pdf」），
    md 落盘侧 safe_name 已 strip；大小写差异（Redis/redis）同样存在。"""
    return stem.strip().lower()


def base_stem(stem: str) -> str:
    """剥 `<stem>_<hash8>`（reconvert 单下划线）/ `<stem>__<hash8>`（backfill 双下划线）
    同名冲突后缀。双下划线必须先判且判两位，否则剥成 `stem_` 留尾（2026-11-17 审核修复，
    同 build_db._base_stem）。"""
    if len(stem) > 10 and stem[-10] == "_" and stem[-9] == "_" and \
            all(c in "0123456789abcdef" for c in stem[-8:]):
        return stem[:-10]
    if len(stem) > 9 and stem[-9] == "_" and \
            all(c in "0123456789abcdef" for c in stem[-8:]):
        return stem[:-9]
    return stem


def mirror_hash_of(md_rel: str) -> str:
    """md 镜像行的 doc_hash 口径：sha256("md-mirror:"+rel)[:16]（extract/build_db 同款）。"""
    return hashlib.sha256(("md-mirror:" + md_rel).encode("utf-8")).hexdigest()[:16]


def load_skip_log() -> dict[str, str]:
    """meta/skip_log.json → {rel: reason}。rel 可填源相对路径或 md 相对路径（均 posix）。

    原因必须是 SKIP_REASONS 之一；`duplicate_of` 还必须带目标（note/target），否则记成
    `duplicate_of:no_target`，由 G1/G2 当无效豁免报 FAIL——空串/拼错原因不再静默放行。
    """
    f = config.META_DIR / "skip_log.json"
    if not f.exists():
        return {}
    data = json.loads(f.read_text(encoding="utf-8"))
    out: dict[str, str] = {}
    for x in data.get("items", []):
        rel = x["rel"].replace("\\", "/")
        reason = (x.get("reason") or "").strip()
        if reason == "duplicate_of" and not (x.get("target") or x.get("note")):
            reason = "duplicate_of:no_target"
        out[rel] = reason
    return out


def skip_reason_ok(reason: str | None, allowed: tuple[str, ...] = SKIP_REASONS) -> bool:
    return bool(reason) and reason in allowed


def _gate(gid: str, name: str, level: str, state: str, fails: list, detail: dict, sql: str) -> dict:
    """门禁结论必须带 SQL 与口径（docs/15 §2.1-5）。"""
    return {"id": gid, "name": name, "level": level, "state": state,
            "fails": fails, "detail": detail, "sql": sql}


def _rel_to_md_map() -> dict[str, str]:
    """源 rel → md_rel 精确映射（backfill log ok 行 + gate_report 落盘行）。

    G1 原只用 stem 首中链接，同 stem 多源（华为校招面经.pdf 在多目录）会链错源
    → mirror hash 判错 → 误报 FAIL（2026-11-17 chunker 增强复核时抓到 28 件）。
    """
    build = Path(config.BUILD_DIR)
    m: dict[str, str] = {}
    log_p = build / "backfill_convert_log.json"
    if log_p.exists():
        for l in json.loads(log_p.read_text(encoding="utf-8")):
            if l.get("ok") and l.get("md"):
                m[l["rel"].replace("\\", "/")] = l["md"].replace("\\", "/")
    gate_p = build / "gate_report.json"
    if gate_p.exists():
        for r in json.loads(gate_p.read_text(encoding="utf-8")).get("rows", []):
            md = r.get("md")
            if not md:
                continue
            try:
                md_rel = Path(md).relative_to(config.QUESTION_MD_DIR).as_posix()
            except ValueError:
                continue
            m[r["rel"].replace("\\", "/")] = md_rel
    return m


# ---------------- G1 源-库全覆盖 ----------------
def g1_coverage(kb_conn, out_conn, skip: dict[str, str]) -> dict:
    """内容池每篇（classified/processing/processed）：doc_contents 有行 且 有题；否则须在 skip_log 登记原因。

    md 镜像关联口径：源 file_name stem 剥 hash 后缀 ↔ doc_contents.md_rel stem 剥后缀
    （build_db 的 by_stem 同款；同名多源取首个是已知局限，报告 detail 注明）。
    """
    pool_sql = f"status IN {status.sql_in(status.INGESTED)}"
    sql = (f"SELECT doc_hash,file_name,dir_path,src_path FROM source_documents WHERE {pool_sql}; "
           "-- 有题判定: questions.doc_hash ∈ {源doc_hash} ∪ {md镜像mirror_hash}")
    rows = kb_conn.execute(
        "SELECT doc_hash, file_name, dir_path, src_path, category, ext "
        f"FROM source_documents WHERE {pool_sql}").fetchall()
    dc_doc = {r[0] for r in out_conn.execute(
        "SELECT DISTINCT doc_hash FROM doc_contents WHERE doc_hash IS NOT NULL")}
    q_docs = {r[0] for r in out_conn.execute("SELECT DISTINCT doc_hash FROM questions")}
    md_rels_by_stem: dict[str, list[str]] = {}
    for (rel,) in out_conn.execute("SELECT DISTINCT md_rel FROM doc_contents"):
        # base_stem 剥 __hash8/_hash8 后缀 + norm_stem 归一空白/大小写，缺一不可
        md_rels_by_stem.setdefault(norm_stem(base_stem(Path(rel).stem)), []).append(rel)
    db_mds = {x for rels in md_rels_by_stem.values() for x in rels}

    fails, n_skip = [], 0
    exact_map = _rel_to_md_map()
    n_exact = 0
    for r in rows:
        rel = f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"]
        stem = norm_stem(base_stem(Path(r["file_name"]).stem))
        # 优先用转换日志/门禁报告里的精确源→md 映射；找不到才退回同名族。
        # 同名多源时族匹配会把"另一份源转出的 md"算成本源已覆盖（review HIGH）。
        exact = exact_map.get(rel.replace("\\", "/"))
        if exact and exact in db_mds:
            md_rels = [exact]
            n_exact += 1
        else:
            md_rels = md_rels_by_stem.get(stem, [])
        mirrors = [mirror_hash_of(x) for x in md_rels]
        has_doc = (r["doc_hash"] in dc_doc) or bool(md_rels)
        has_q = (r["doc_hash"] in q_docs) or any(m in q_docs for m in mirrors)
        if has_doc and has_q:
            continue
        if rel in skip:
            why = skip[rel]
            if skip_reason_ok(why):
                n_skip += 1
                continue
            fails.append({"rel": rel, "file_name": r["file_name"], "ext": r["ext"],
                          "category": r["category"], "skip_reason": why,
                          "suggest": "skip_log 原因非法：改成 SKIP_REASONS 之一"})
            continue
        fails.append({"rel": rel, "file_name": r["file_name"], "ext": r["ext"],
                      "category": r["category"],
                      "has_doc_contents": "Y" if has_doc else "N",
                      "has_questions": "Y" if has_q else "N",
                      "suggest": "backfill_convert --convert" if not has_doc else "extract 重跑"})
    state = "PASS" if not fails else "FAIL"
    return _gate("G1", "源-库全覆盖", "硬", state, fails,
                 {"classified": len(rows), "skipped_by_log": n_skip,
                  "exact_rel_mapped": n_exact,
                  "note": "优先精确源→md 映射（backfill/gate_report），找不到才按同名族判定"}, sql)


# ---------------- G2 转换链闭环 ----------------
def g2_chain(out_conn, md_root: Path, ocr_root: Path, skip: dict[str, str]) -> dict:
    """三段链：md→doc_contents、ocr_text→md、doc_contents→questions。"""
    sql = ("磁盘 rglob('*.md') vs doc_contents.md_rel; "
           "build/ocr_text/<rel>.txt vs md stem; "
           "doc_contents(doc_hash,mirror_hash) vs questions.doc_hash 计数")
    fails = []
    # ① 磁盘 md 未进 doc_contents
    disk_mds = {p.relative_to(md_root).as_posix() for p in md_root.rglob("*.md")} \
        if md_root.exists() else set()
    db_mds = {r[0] for r in out_conn.execute("SELECT DISTINCT md_rel FROM doc_contents")}
    for rel in sorted(disk_mds - db_mds):
        if skip_reason_ok(skip.get(rel)):
            continue
        fails.append({"rel": rel, "kind": "md_not_in_db", "suggest": "build_db 重建"})

    # ② ocr_text 有全文却无 md（350道 事故回归样本）
    if ocr_root.exists():
        md_stems = {norm_stem(base_stem(p.stem)) for p in md_root.rglob("*.md")} if md_root.exists() else set()
        for txt in sorted(ocr_root.rglob("*.txt")):
            src_rel = txt.relative_to(ocr_root).as_posix()
            if src_rel.endswith(".txt"):
                src_rel = src_rel[:-4]
            if norm_stem(base_stem(Path(src_rel).stem)) in md_stems:
                continue
            if skip_reason_ok(skip.get(src_rel)) or skip_reason_ok(skip.get(Path(src_rel).name)):
                continue
            fails.append({"rel": src_rel, "kind": "ocr_text_no_md",
                          "suggest": "backfill_convert --merge-ocr / ocr_merge"})

    # ③ doc_contents 有行却 0 题且未登记原因（no_question=真无题；chunker_no_split=讲解型 md
    #    切不出，chunker 增强前如实记账，docs/15「skip_log 是一等公民」）
    ok_skip = {"no_question", "chunker_no_split"}
    for md_rel, doc_hash, mirror in out_conn.execute(
            "SELECT md_rel, doc_hash, mirror_hash FROM doc_contents"):
        n = out_conn.execute(
            "SELECT COUNT(*) FROM questions WHERE doc_hash IN (?, ?)",
            (doc_hash, mirror)).fetchone()[0]
        if n == 0 and skip.get(md_rel) not in ok_skip:
            fails.append({"rel": md_rel, "kind": "md_zero_questions", "suggest": "extract 重跑"})
    state = "PASS" if not fails else "FAIL"
    return _gate("G2", "转换链闭环", "硬", state, fails,
                 {"disk_mds": len(disk_mds), "db_mds": len(db_mds)}, sql)


# ---------------- G3 主题唯一口径 ----------------
def g3_topic(out_conn) -> dict:
    """topic 空串/'未分类' 计数须为 0；两种表示混用即口径问题（K12 待统一）。"""
    sql = ("SELECT COUNT(*) FROM questions WHERE topic='' OR topic IS NULL; "
           "SELECT COUNT(*) FROM questions WHERE topic='未分类';")
    empty = out_conn.execute(
        "SELECT COUNT(*) FROM questions WHERE topic='' OR topic IS NULL").fetchone()[0]
    uncls = out_conn.execute(
        "SELECT COUNT(*) FROM questions WHERE topic='未分类'").fetchone()[0]
    fails = []
    if empty:
        fails.append({"kind": "topic_empty", "count": empty})
    if uncls:
        fails.append({"kind": "topic_unclassified", "count": uncls})
    if empty and uncls:
        fails.append({"kind": "mixed_representation", "note": "空串与'未分类'混用，先统一口径(K12)"})
    state = "PASS" if not fails else "FAIL"
    return _gate("G3", "主题唯一口径", "硬", state, fails,
                 {"empty": empty, "unclassified": uncls}, sql)


# ---------------- G4 索引一致性 ----------------
def g4_index(out_conn) -> dict:
    """vec_questions / questions_fts 与 questions 行数与 qid 集合一致。"""
    sql = ("SELECT COUNT(*) FROM questions; SELECT COUNT(*) FROM vec_questions; "
           "SELECT rowid FROM vec_questions; SELECT COUNT(*) FROM questions_fts;")
    n_q = out_conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
    has_vec = out_conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='vec_questions'").fetchone()[0] > 0
    if not has_vec:
        return _gate("G4", "索引一致性", "硬", "WARN", [],
                     {"questions": n_q, "note": "vec_questions 未建（embed_questions.py 未跑），向量层可选"},
                     sql)
    try:
        n_vec = out_conn.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
        vec_ids = {r[0] for r in out_conn.execute("SELECT rowid FROM vec_questions")}
    except Exception as ex:  # noqa: BLE001 —— vec0 扩展未加载（sqlite_vec 不可用）时无法校验
        return _gate("G4", "索引一致性", "硬", "FAIL", [
            {"kind": "vec_unavailable", "error": f"{type(ex).__name__}: {ex}",
             "note": "vec_questions 存在但 sqlite_vec 扩展未加载：硬门禁不能降级 WARN 放行"}],
            {"questions": n_q, "note": "向量表存在但扩展不可用"}, sql)
    q_ids = {r[0] for r in out_conn.execute("SELECT qid FROM questions")}
    n_fts = out_conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
    fts_ids = {r[0] for r in out_conn.execute("SELECT rowid FROM questions_fts")}
    fails = []
    if n_vec != n_q or vec_ids != q_ids:
        fails.append({"kind": "vec_mismatch", "questions": n_q, "vec": n_vec,
                      "qid_diff": len(q_ids ^ vec_ids)})
    if n_fts != n_q or fts_ids != q_ids:
        fails.append({"kind": "fts_mismatch", "questions": n_q, "fts": n_fts,
                      "rowid_diff": len(q_ids ^ fts_ids)})
    state = "PASS" if not fails else "FAIL"
    return _gate("G4", "索引一致性", "硬", state, fails,
                 {"questions": n_q, "vec": n_vec, "fts": n_fts}, sql)


# ---------------- G5 台账-磁盘一致 ----------------
MAX_CHAIN = 3


def _successor_rows(kb_conn, target: dict) -> tuple[str, list]:
    """converted_to 目标 → (解析方式, 候选行列表)。候选多于 1 条即无法判定。"""
    digest = target.get("hash")
    if digest:
        row = kb_conn.execute(
            "SELECT doc_hash, status, note, file_name FROM source_documents WHERE doc_hash=?",
            (digest,)).fetchone()
        return "hash", ([row] if row else [])
    name = target.get("name")
    if not name:
        return "none", []
    if target.get("dir") is not None:
        return "dir", kb_conn.execute(
            "SELECT doc_hash, status, note, file_name FROM source_documents "
            "WHERE file_name=? AND dir_path=?", (name, target["dir"])).fetchall()
    return "name", kb_conn.execute(
        "SELECT doc_hash, status, note, file_name FROM source_documents "
        "WHERE file_name=?", (name,)).fetchall()


def _successor_row(kb_conn, target: dict) -> tuple[dict | None, str]:
    """converted_to 目标 → (台账后继行, 失败原因)。

    新格式带 hash：`<dir>/<name>#<docx_hash>`，hash 就是 docx 记录的 doc_hash，
    直接按 doc_hash 命中，比"文件名 + 目录"更硬（同名不同目录不会被误认）。
    旧格式裸 `<name>`（T19）：只能按文件名找；同名多条时**不能**随便挑一条，
    否则 G5 会因为别的目录里同名文件合格而假过——按 `ambiguous` 判无法解析。
    """
    how, rows = _successor_rows(kb_conn, target)
    if how == "none":
        return None, "no_target_name"
    if not rows:
        return None, "missing"
    if len(rows) > 1:
        return None, "ambiguous"          # 裸格式同名多份，无法确认是哪一条后继
    good = [r for r in rows if r["status"] in status.SUCCESSOR_OK]
    if good:
        return good[0], ""
    # 只有被退役的候选——可能是一条接替链（.doc → .docx → .pdf），继续往下追
    return rows[0], ""


def converted_target_reason(kb_conn, target: dict, depth: int = 0) -> str:
    """后继能解析到有效态返回空串，否则返回失败原因（供 G5 明细用）。"""
    row, why = _successor_row(kb_conn, target)
    if row is None:
        return why
    if row["status"] in status.SUCCESSOR_OK:
        return ""
    if row["status"] not in (status.SUPERSEDED,) or depth >= MAX_CHAIN:
        return f"chain_unsolved:{row['status']}"
    nxt = events.converted_targets(kb_conn, row["doc_hash"], row["note"])
    if not nxt:
        return "chain_no_target"
    reasons = [converted_target_reason(kb_conn, t, depth + 1) for t in nxt]
    return "" if any(not r for r in reasons) else reasons[0]


def converted_target_ok(kb_conn, target: dict, depth: int = 0) -> bool:
    """后继记录存在且最终落在有效态（允许有限跳数的接替链）。"""
    return not converted_target_reason(kb_conn, target, depth)


def audit_converted(kb_conn) -> list[dict]:
    """盘点全库 superseded 行的 converted_to 是否都能解析到有效后继（`--audit-converted`）。"""
    out = []
    for r in kb_conn.execute(
            "SELECT doc_hash, file_name, dir_path, note FROM source_documents "
            f"WHERE status='{status.SUPERSEDED}' ORDER BY dir_path, file_name").fetchall():
        targets = events.converted_targets(kb_conn, r["doc_hash"], r["note"])
        out.append({
            "doc_hash": r["doc_hash"],
            "rel": f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"],
            "targets": targets,
            "resolved": bool(targets) and all(converted_target_ok(kb_conn, t) for t in targets),
            "legacy": any(not t.get("hash") for t in targets),
            "no_target": not targets,
        })
    return out


def _trace_ok(note: str, rel: str, retire_rels: set[str], retire_names: set[str]) -> bool:
    """退役留痕判定：必须是可核对的记录，不是"note 非空即过"。

    接受：converted_to 事件/子串、明确的退役/物理删除/移出语料根措辞、retire_log 的**完整相对路径**。
    retire_log 只有文件名时（老格式）才退回同名匹配；通用文件名（README.md）不再单独放行。
    """
    text = (note or "").strip()
    markers = ("converted_to", "退役", "物理删除", "移出语料根", "quarantine", "superseded",
               "乱码", "替代")
    if text and any(k in text for k in markers):
        return True
    rel_posix = rel.replace("\\", "/")
    if rel_posix in retire_rels:
        return True
    return bool(retire_names) and not retire_rels and Path(rel_posix).name in retire_names


def g5_disk(kb_conn, corpus_root: Path, build_dir: Path) -> dict:
    """classified 在盘；superseded 在 99 区或已删且留痕；99 区每件在台账。

    converted_to 校验（T12/T19）：读 `document_events`，老库退回 note 子串；
    新格式按 hash 精确命中后继记录，历史裸格式按文件名解析——两者都必须落到
    有效态（非 absent/superseded/quarantined）或一条有限的接替链末端。
    """
    sql = ("classified: src_path exists; superseded: dir_path LIKE '99-%' OR 留痕(note/retire_log); "
           "converted_to: 后继记录存在且未退役; "
           "99区: sha256(file)[:16] ∈ source_documents.doc_hash")
    fails = []
    retire_names: set[str] = set()
    retire_rels: set[str] = set()
    retire_log = build_dir / "retire_log.csv"
    if retire_log.exists():
        with retire_log.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 3 and row[2].strip():
                    retire_names.add(Path(row[2].strip()).name)
                if len(row) >= 4 and row[3].strip():
                    retire_rels.add(row[3].strip().replace("\\", "/"))

    legacy_checked = 0
    for r in kb_conn.execute(
            "SELECT doc_hash, file_name, dir_path, src_path, status, note "
            f"FROM source_documents WHERE status IN {status.sql_in(status.G5_SCAN)}").fetchall():
        src = Path(r["src_path"]) if (r["src_path"] or "").strip() else None
        rel = f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"]
        if r["status"] in (status.CLASSIFIED, status.PROCESSING, status.PROCESSED):
            # Path("") == Path(".")，空 src_path 会被 exists() 判成"在盘"——必须先判空
            if src is None or not src.exists():
                fails.append({"kind": "classified_src_missing", "rel": r["file_name"]})
            continue
        # superseded：在盘则须在 99 区，或 note 有留痕（converted_to 原地转换 /
        # review_cli 退役确认——"已退役待物理清理"是合法中间态，docs/15 G5 意图=有记录即过）；
        # 不在盘则同样须有留痕（note / retire_log）。
        # converted_to 额外校验后继记录：否则"原 .doc 已 superseded、docx 根本没入库"，
        # purge 的 superseded_no_replacement 会虚高。
        targets = events.converted_targets(kb_conn, r["doc_hash"], r["note"])
        for t in targets:
            if t.get("hash"):
                legacy = False
            else:
                legacy = True
                legacy_checked += 1
            why = converted_target_reason(kb_conn, t)
            if why:
                fails.append({"kind": "converted_target_missing", "rel": r["file_name"],
                              "target": t, "legacy": legacy, "why": why})
        if src is not None and src.exists():
            in_99 = (r["dir_path"] or "").startswith(config.QUARANTINE_PREFIX) \
                    or config.QUARANTINE_PREFIX in str(src)
            if not (in_99 or _trace_ok(r["note"], rel, retire_rels, retire_names)):
                fails.append({"kind": "superseded_not_quarantined", "rel": r["file_name"]})
        elif not _trace_ok(r["note"], rel, retire_rels, retire_names):
            fails.append({"kind": "superseded_no_trace", "rel": r["file_name"]})

    q_dir = corpus_root / config.QUARANTINE_PREFIX
    if q_dir.exists():
        known = {r[0] for r in kb_conn.execute("SELECT doc_hash FROM source_documents")}
        for p in q_dir.rglob("*"):
            if not p.is_file():
                continue
            rel99 = p.relative_to(corpus_root).as_posix()
            if rel99 in retire_rels:
                continue          # 退役 md 镜像（retire_log 完整路径留痕，台账不登记派生件）
            if p.name in retire_names and not retire_rels:
                continue          # 老格式只有文件名，才允许按名放行
            if hashing.sha256_file(p)[:16] not in known:
                fails.append({"kind": "orphan_in_99", "rel": rel99})
    state = "PASS" if not fails else "FAIL"
    return _gate("G5", "台账-磁盘一致", "硬", state, fails,
                 {"retire_log_rows": len(retire_names), "legacy_converted_checked": legacy_checked},
                 sql)


# ---------------- G6 打标质量 ----------------
G6_QTYPES = labelspec.qtypes()      # T21：枚举单一来源（meta/label_schema.yaml）


def _json_list_ok(raw, allowed: tuple[str, ...], cap: int) -> bool:
    """多选列校验：空/None 放行（历史行没有该维度），有值则必须是 allowed 内的 JSON 数组且不超上限。"""
    if raw is None or raw == "":
        return True
    try:
        vals = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(vals, list) or len(vals) > cap:
        return False
    return all(isinstance(v, str) and v in allowed for v in vals)


def g6_label(kb_conn) -> dict:
    """打标质量（docs/15 §2 G6，2026-11-17 落地；T21 扩维度）。

    staging 无行 → SKIP（打标未启用）；有行则：
      硬指标（FAIL 阻断 merge）：schema 合格率 ≥ 0.95——
        topic 非空且 ≤40 字、qtype ∈ 枚举、difficulty ∈ 1-5、tags 可解析为 JSON 数组；
        T21 新维度：position ∈ 岗位枚举（≤2）、quality ∈ 质量枚举（≤3）、
        canonical_q 非空且 ≤60 字——**只在有值时校验**（历史 staging 行没有这些列）。
      软指标（WARN 记录）：staging.topic 分布相对 questions 现有分布漂移——
        新 topic（现有库未出现过的主题）占比 > 15% → WARN（可能是打标跑偏，人工抽查）；
        T21 新维度覆盖率与标签归一命中数进 detail（可见、不阻断）。
    人工抽检环节（≥0.8 准确率）由 `batch_label --sample 50` 出清单人工核，无 ground truth 不自动判。
    """
    sql = ("SELECT COUNT(*) FROM labels_staging; "
           "-- 合格: topic非空<=40字 AND qtype∈枚举 AND difficulty∈1..5 AND tags为JSON数组 "
           "AND position/quality 有值时合法 AND canonical_q 有值时<=60字")
    n = kb_conn.execute("SELECT COUNT(*) FROM labels_staging").fetchone()[0]
    if n == 0:
        return _gate("G6", "打标质量", LEVEL_HARD, "SKIP", [],
                     {"staged": 0, "note": "labels_staging 为空（打标未跑或已 merge）"}, sql)
    rows = kb_conn.execute(
        "SELECT s.topic, s.qtype, s.difficulty, s.tags, s.position, s.quality, "
        "       s.canonical_q, s.tag_norm_hits FROM labels_staging s").fetchall()
    pos_vals = labelspec.positions()
    qual_vals = labelspec.quality_flags()
    pos_cap = int(labelspec.field("position").get("max_items") or 2)
    qual_cap = int(labelspec.field("quality").get("max_items") or 3)
    canon_max = labelspec.canonical_max_len()
    bad = []
    canon_filled = pos_filled = qual_filled = 0
    norm_hits = 0
    for r in rows:
        why = []
        if not (r["topic"] or "").strip() or len(r["topic"]) > 40:
            why.append("topic")
        if r["qtype"] not in G6_QTYPES:
            why.append("qtype")
        if not isinstance(r["difficulty"], int) or not 1 <= r["difficulty"] <= 5:
            why.append("difficulty")
        try:
            tags = json.loads(r["tags"] or "[]")
            if not isinstance(tags, list):
                why.append("tags")
        except json.JSONDecodeError:
            why.append("tags")
        if not _json_list_ok(r["position"], pos_vals, pos_cap):
            why.append("position")
        if not _json_list_ok(r["quality"], qual_vals, qual_cap):
            why.append("quality")
        canon = (r["canonical_q"] or "").strip()
        if r["canonical_q"] is not None and (not canon or len(canon) > canon_max):
            why.append("canonical_q")
        if canon:
            canon_filled += 1
        if r["position"]:
            pos_filled += 1
        if r["quality"]:
            qual_filled += 1
        norm_hits += int(r["tag_norm_hits"] or 0)
        if why:
            bad.append({"topic": r["topic"], "qtype": r["qtype"],
                        "difficulty": r["difficulty"], "bad_fields": why})
    ok_rate = 1 - len(bad) / n

    # 软指标：新 topic 占比。基线优先用封闭词表（批次三 R2，2026-11-19）——词表封闭后
    # drift 捕捉的是 prompt 锁失败（词表外主题漏网）；词表不可用退回 questions 现有分布。
    known = set(topic_vocab())
    if known:
        drift_baseline = "vocab"
    else:
        known = {r[0] for r in kb_conn.execute(
            "SELECT DISTINCT topic FROM questions WHERE topic IS NOT NULL AND topic != ''")}
        drift_baseline = "questions"
    if known:
        new_topics = {r["topic"] for r in rows if (r["topic"] or "").strip()} - known
        n_new = sum(1 for r in rows if (r["topic"] or "").strip() in new_topics)
        drift = n_new / n
    else:
        # F8/bug1（2026-11-19）：首批打标时 questions 的 topic 必然全空（无基线），
        # 漂移指标无意义——原实现此时 drift=1.0 必 WARN，属误报。跳过并留痕。
        drift = 0.0
        drift_baseline = "none(首批)"

    fails = []
    state = "PASS"
    if ok_rate < 0.95:
        state = "FAIL"
        fails.extend(bad[:20])
        fails.append({"kind": "schema_ok_rate", "ok_rate": round(ok_rate, 4),
                      "threshold": 0.95, "bad_total": len(bad)})
    elif drift > 0.15:
        state = "WARN"
        fails.append({"kind": "topic_drift", "new_topic_ratio": round(drift, 4),
                      "new_topics": sorted(new_topics)[:10],
                      "note": "新主题占比偏高——--sample 50 人工抽查后再 merge"})
    return _gate("G6", "打标质量", LEVEL_HARD, state, fails,
                 {"staged": n, "ok_rate": round(ok_rate, 4),
                  "new_topic_ratio": round(drift, 4),
                  "drift_baseline": drift_baseline,
                  "coverage": {"canonical_q": canon_filled / n,
                               "position": pos_filled / n,
                               "quality": qual_filled / n},
                  "tag_norm_hits": norm_hits}, sql)


G6B_MIN_N = 50          # 抽检样本小于 50 时点估计没有统计效力（n=20/acc=.70 有 ~24% 概率观测≥.8）
G6B_WEAK_MIN_N = 5      # 主题样本 <5 不进 weak_topics，避免 1 条错就触发 --review


def _wilson_lower(k: int, n: int, z: float = 1.96) -> float:
    """二项比例 Wilson 置信下界（95%）。点估计过线但下界过低时只记为风险，不单独挡门。"""
    if n <= 0:
        return 0.0
    p = k / n
    d = 1 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5)
    return max(0.0, (centre - margin) / d)


def g6b_golden(kb_conn) -> dict:
    """G6b 金标抽检（批次二 G3，2026-11-19——docs/15 G6 规格"抽检 50 题准确率 ≥0.8"
    从人工环节变自动门禁）。

    golden_labels 无表 → SKIP（金标未启用）；无行 → SKIP（还没做金标）。
    与 staging 按 q_norm_hash 求交集后 **matched==0 或 matched<20 → WARN**（样本不足，
    未判，不挡 merge，但必须显式可见——SKIP 会被读成"通过"）；topic_acc < 0.8 → FAIL（阻断 merge）。
    qtype/difficulty 准确率只记录不设阈（docs/13 L6 旧口径 ≥70% 仅供参考）。
    detail 带 weak_topics（acc<0.8 的金标主题，批次三 --review 的复核目标）与
    混淆矩阵 matrix[gold][pred]。"""
    import sqlite3
    sql = ("SELECT g.topic_gold, s.topic FROM golden_labels g "
           "JOIN labels_staging s ON s.q_norm_hash=g.q_norm_hash")
    try:
        n_gold = kb_conn.execute("SELECT COUNT(*) FROM golden_labels").fetchone()[0]
    except sqlite3.OperationalError:
        return _gate("G6b", "金标抽检", "硬", "SKIP", [],
                     {"note": "golden_labels 表不存在（008 迁移未应用）"}, sql)
    if n_gold == 0:
        return _gate("G6b", "金标抽检", "硬", "SKIP", [],
                     {"golden": 0, "note": "golden_labels 无行——先 --golden-sample/--golden-load"}, sql)
    rows = kb_conn.execute(
        "SELECT g.topic_gold, g.qtype_gold, g.difficulty_gold, s.topic, s.qtype, s.difficulty "
        "FROM golden_labels g JOIN labels_staging s ON s.q_norm_hash=g.q_norm_hash").fetchall()
    n = len(rows)
    if n == 0:
        return _gate("G6b", "金标抽检", "硬", "WARN", [],
                     {"golden": n_gold, "matched": 0,
                      "note": "金标与 staging 无交集（staging 已 merge 或非同批）——样本不足，未判"},
                     sql)
    if n < G6B_MIN_N:
        return _gate("G6b", "金标抽检", "硬", "WARN", [],
                     {"golden": n_gold, "matched": n,
                      "note": f"样本 {n} < {G6B_MIN_N} 不足以判准确率——样本不足，未判（不等于通过）"},
                     sql)
    n_ok = sum(1 for r in rows if r["topic"] == r["topic_gold"])
    topic_acc = n_ok / n
    topic_lb = _wilson_lower(n_ok, n)
    qt = [r for r in rows if r["qtype_gold"]]
    qtype_acc = (sum(1 for r in qt if r["qtype"] == r["qtype_gold"]) / len(qt)) if qt else None
    df = [r for r in rows if r["difficulty_gold"]]
    diff_acc = (sum(1 for r in df if r["difficulty"] == r["difficulty_gold"]) / len(df)) if df else None
    by_topic: dict[str, list[bool]] = {}
    matrix: dict[str, dict[str, int]] = {}
    for r in rows:
        by_topic.setdefault(r["topic_gold"], []).append(r["topic"] == r["topic_gold"])
        matrix.setdefault(r["topic_gold"], {})
        matrix[r["topic_gold"]][r["topic"]] = matrix[r["topic_gold"]].get(r["topic"], 0) + 1
    weak = sorted(t for t, oks in by_topic.items()
                  if len(oks) >= G6B_WEAK_MIN_N and sum(oks) / len(oks) < 0.8)
    fails, state = [], "PASS"
    if topic_acc < 0.8:
        state = "FAIL"
        fails.append({"kind": "golden_topic_acc", "topic_acc": round(topic_acc, 4),
                      "threshold": 0.8, "weak_topics": weak})
    return _gate("G6b", "金标抽检", "硬", state, fails,
                 {"golden": n_gold, "matched": n, "topic_acc": round(topic_acc, 4),
                  "topic_acc_lower95": round(topic_lb, 4),
                  "weak_min_n": G6B_WEAK_MIN_N,
                  "qtype_acc": round(qtype_acc, 4) if qtype_acc is not None else None,
                  "difficulty_acc": round(diff_acc, 4) if diff_acc is not None else None,
                  "weak_topics": weak, "matrix": matrix}, sql)


def g8_single_identity(out_conn) -> dict:
    """G8 单身份（硬，P0-1）：有源回链的 md 产物，其 `md-mirror:` 身份下不得再有题行。

    判定：`doc_contents` 里 `doc_hash IS NOT NULL` 的每一行（= 能回链到源文档的产物），
    若 `questions` 仍有 `doc_hash = mirror_hash` 的行 → 说明这份原文又被镜像身份抽了一遍，
    题数/见证数会双份放大（`docs/27` P0-1）。
    正常态：一个源只有一种身份，mirror_rows 全为 0；孤儿 md（doc_hash IS NULL）不在判据内。
    """
    sql = ("SELECT dc.md_rel, dc.doc_hash, dc.mirror_hash, "
           " (SELECT COUNT(*) FROM questions q WHERE q.doc_hash=dc.mirror_hash) AS mirror_rows, "
           " (SELECT COUNT(*) FROM questions q WHERE q.doc_hash=dc.doc_hash) AS source_rows "
           "FROM doc_contents dc WHERE dc.doc_hash IS NOT NULL")
    import sqlite3
    try:
        rows = out_conn.execute(sql).fetchall()
    except sqlite3.OperationalError:
        return _gate("G8", "单身份（双份检测）", "硬", "SKIP", [],
                     {"note": "doc_contents/questions 表缺失（检索库未建）"}, sql)
    bad = [r for r in rows if r["mirror_rows"]]
    mirror_total = sum(r["mirror_rows"] for r in bad)
    fails = [{"kind": "dual_identity", "md": r["md_rel"], "doc_hash": r["doc_hash"],
              "mirror_hash": r["mirror_hash"], "mirror_rows": r["mirror_rows"],
              "source_rows": r["source_rows"]} for r in bad[:20]]
    if len(bad) > 20:
        fails.append({"kind": "dual_identity_more", "bad_total": len(bad)})
    return _gate("G8", "单身份（双份检测）", "硬", "FAIL" if bad else "PASS", fails,
                 {"mapped_products": len(rows), "bad_products": len(bad),
                  "mirror_rows_total": mirror_total,
                  "note": "有源回链的产物若还挂 mirror 题行 = 同一原文抽了两遍"}, sql)


# ---------------- G7 文档一致性（软） ----------------
def g7_docs(repo_root: Path, actual_tests: int) -> dict:
    """README 测试数 vs 实际；尚未实现清单不得列已存在脚本；README 引用的 docs 路径必须存在。"""
    warns = []
    readme_p = repo_root / "README.md"
    readme = readme_p.read_text(encoding="utf-8") if readme_p.exists() else ""
    m = re.search(r"tests/[^\n]*?(\d+)\s*个用例", readme)
    if m and int(m.group(1)) != actual_tests:
        warns.append({"kind": "tests_count_stale",
                      "readme": int(m.group(1)), "actual": actual_tests})
    # README 里的 `docs/...md` 必须真实存在：归档/改名后最容易留下死链（2026-11-19 R3 归档）
    for ref in sorted(set(re.findall(r"`?(docs/[\w./\-]+\.md)`?", readme))):
        if not (repo_root / ref).exists():
            warns.append({"kind": "readme_doc_missing", "path": ref})
    # 「尚未实现」节的列表项不得列 mianjing_kb/ 已存在的脚本（`- ~~` 删除线=已落地条目，跳过；
    # 正文括号里的脚本引用不在行首列表项上，不构成"声称未实现"）
    sec = re.search(r"## 尚未实现.*?(?=\n## |\Z)", readme, re.S)
    if sec:
        for line in sec.group(0).splitlines():
            s = line.strip()
            if not s.startswith("-") or s.startswith("- ~~"):
                continue
            m = re.search(r"`([\w/]+\.py)`", s)
            if m and (repo_root / "mianjing_kb" / m.group(1)).exists():
                warns.append({"kind": "unimplemented_listed_but_exists", "script": m.group(1)})
    # docs 编号范围：README 目录树 docs 行的 "~ N" 上界 vs docs/ 实际最大编号
    m = re.search(r"docs/[^\n]*?~\s*(\d+)", readme)
    if m:
        nums = [int(m2.group(1)) for p in (repo_root / "docs").glob("*.md")
                if (m2 := re.match(r"(\d+)-", p.name))]
        if nums and int(m.group(1)) < max(nums):
            warns.append({"kind": "docs_index_stale",
                          "readme_upto": int(m.group(1)), "actual_max": max(nums)})
    state = "PASS" if not warns else "WARN"
    return _gate("G7", "文档一致性", "软", state, warns, {"actual_tests": actual_tests}, "")


def _collect_tests(repo_root: Path) -> int:
    """pytest --collect-only 实际用例数（G7 用；失败返回 -1 不阻断）。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests", "--collect-only", "-q"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=120)
        m = re.search(r"(\d+)\s+tests?\s+collected", proc.stdout)
        return int(m.group(1)) if m else -1
    except Exception:  # noqa: BLE001
        return -1


def _ro_uri(path: Path | str) -> str:
    """只读 SQLite URI：用 as_uri() 做百分号编码，避免路径里的空格/#/? 破坏 URI。"""
    return Path(path).resolve().as_uri() + "?mode=ro"


def _db_identity(path: Path | str) -> dict:
    p = Path(path)
    try:
        st = p.stat()
        return {"path": str(p), "size_bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
    except OSError:
        return {"path": str(p), "missing": True}


def write_report(build_dir: Path, gates: list[dict], elapsed: float,
                 identity: dict | None = None) -> tuple[Path, Path]:
    """verify_report.json / .md 落盘。identity 绑定数据库版本，供消费方拒绝过期报告。"""
    hard_fail = [g for g in gates if g["level"] == "硬" and g["state"] == "FAIL"]
    summary = {
        "at": datetime.now().strftime(NOW_FMT),
        "elapsed_sec": round(elapsed, 1),
        "hard_fail": len(hard_fail),
        "verdict": "FAIL" if hard_fail else "PASS",
        "gates": {g["id"]: g["state"] for g in gates},
        "db_identity": identity or {},
    }
    js = build_dir / "verify_report.json"
    js.write_text(json.dumps({"summary": summary, "gates_detail": gates},
                             ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# 核账门禁报告（{summary['at']}）", "",
             f"- 结论：**{summary['verdict']}**（硬门禁 FAIL {len(hard_fail)} 道，耗时 {summary['elapsed_sec']}s）",
             f"- 数据库指纹：`{json.dumps(summary['db_identity'], ensure_ascii=False)}`",
             "- 口径：G1-G5 刷新后必跑；G6/G6b 打标后；G7 pre-commit（--docs）", ""]
    for g in gates:
        n = len(g["fails"])
        lines.append(f"## {g['id']} {g['name']} — {g['state']}（{g['level']}，问题 {n} 项）")
        if g["sql"]:
            lines.append(f"- SQL/口径：`{g['sql'][:200]}`")
        if g["detail"]:
            lines.append(f"- 明细：{json.dumps(g['detail'], ensure_ascii=False)}")
        for f in g["fails"][:10]:
            lines.append(f"  - {json.dumps(f, ensure_ascii=False)}")
        if n > 10:
            lines.append(f"  - …共 {n} 项，全量见 verify_report.json")
        lines.append("")
    md = build_dir / "verify_report.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    return js, md


def write_backfill_plan(build_dir: Path, gates: list[dict]) -> Path:
    """G1/G2 FAIL 项 → build/backfill_plan.csv（门禁即清单，docs/15 §2.1-3）。

    只收文件级补录项；G3-G8 的结论不是"某文件该补什么"，混进来会让清单行数虚高。
    """
    plan = []
    for g in gates:
        if g["id"] not in ("G1", "G2"):
            continue
        for f in g["fails"]:
            plan.append({"gate": g["id"], **f})
    out = build_dir / "backfill_plan.csv"
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["gate", "rel", "file_name", "ext", "category",
                                           "has_doc_contents", "has_questions", "kind", "suggest"])
        w.writeheader()
        for row in plan:
            w.writerow({k: row.get(k, "") for k in w.fieldnames})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="G1-G8/G6b 核账门禁（任一硬门禁 FAIL 即非 0 退出）")
    ap.add_argument("--docs", action="store_true", help="加跑 G7 文档一致性（pre-commit 用）")
    ap.add_argument("--audit-converted", action="store_true",
                    help="只盘点 superseded 行的 converted_to 能否解析到有效后继（T19）")
    ap.add_argument("--kb-db", default=str(config.DB_PATH), help="台账 kb.sqlite")
    ap.add_argument("--out-db", default=str(Path(config.BUILD_DIR) / "面经库.db"),
                    help="检索库 面经库.db")
    args = ap.parse_args(argv)

    import sqlite3
    started = time.perf_counter()
    kb_conn = sqlite3.connect(_ro_uri(args.kb_db), uri=True)
    kb_conn.row_factory = sqlite3.Row
    if args.audit_converted:
        rows = audit_converted(kb_conn)
        unresolved = [r for r in rows if not r["resolved"]]
        # 只把"有目标却解析不到"算问题：99 区里 superseded 且从未写过 converted_to 的行
        # 本来就没有后继（被覆盖/被删/纯重复），它们进 no_target_rows 供人工抽查，不算失败。
        bad = [r for r in unresolved if r["targets"]]
        print(json.dumps({
            "superseded_rows": len(rows),
            "resolved": len(rows) - len(unresolved),
            "unresolved": len(unresolved),
            "unresolved_with_target": len(bad),
            "legacy_rows": sum(1 for r in rows if r["legacy"]),
            "no_target_rows": sum(1 for r in rows if r["no_target"]),
            "unresolved_detail": bad[:50],
        }, ensure_ascii=False, indent=1))
        kb_conn.close()
        return 1 if bad else 0
    out_conn = sqlite3.connect(_ro_uri(args.out_db), uri=True)
    out_conn.row_factory = sqlite3.Row
    try:  # vec_questions 是 vec0 虚表，校验它需要 sqlite_vec 扩展（可用则加载，G4 自行降级 WARN）
        import sqlite_vec
        out_conn.enable_load_extension(True)
        sqlite_vec.load(out_conn)
        out_conn.enable_load_extension(False)
    except ImportError:
        pass
    skip = load_skip_log()
    build_dir = Path(config.BUILD_DIR)

    gates = [
        g1_coverage(kb_conn, out_conn, skip),
        g2_chain(out_conn, Path(config.QUESTION_MD_DIR), build_dir / "ocr_text", skip),
        g3_topic(out_conn),
        g4_index(out_conn),
        g5_disk(kb_conn, Path(config.CORPUS_ROOT), build_dir),
        g6_label(kb_conn),
        g6b_golden(kb_conn),
        g8_single_identity(out_conn),
    ]
    if args.docs:
        repo_root = Path(__file__).resolve().parents[1]
        gates.insert(-1, g7_docs(repo_root, _collect_tests(repo_root)))
    kb_conn.close()
    out_conn.close()

    identity = {"kb": _db_identity(args.kb_db), "out": _db_identity(args.out_db)}
    js, md = write_report(build_dir, gates, time.perf_counter() - started, identity)
    plan = write_backfill_plan(build_dir, gates)
    hard_fail = [g for g in gates if g["level"] == "硬" and g["state"] == "FAIL"]
    print(json.dumps({
        "verdict": "FAIL" if hard_fail else "PASS",
        "gates": {g["id"]: g["state"] for g in gates},
        "hard_fail": [g["id"] for g in hard_fail],
        "fails_total": sum(len(g["fails"]) for g in gates),
        "backfill_plan_rows": sum(len(g["fails"]) for g in gates if g["id"] in ("G1", "G2")),
        "out_json": str(js), "out_md": str(md), "out_plan": str(plan),
    }, ensure_ascii=False, indent=1))
    return 1 if hard_fail else 0


if __name__ == "__main__":
    raise SystemExit(main())

