# -*- coding: utf-8 -*-
"""verify_ledger：G1-G7 核账门禁（docs/15 §2 方案，2026-11-17 审核后落地）。

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
  G7 文档一致性（软，--docs）：README 声称的测试数 vs pytest --collect-only 实际；
     「尚未实现」清单不得列 tools/ 已存在的脚本；docs 编号范围 vs docs/ 实际

输出（build/）：
  verify_report.json / verify_report.md   任一硬门禁 FAIL 即非 0 退出
  backfill_plan.csv                        G1/G2 FAIL 项即补录清单（门禁即清单）

用法：
    python tools/verify_ledger.py            # G1-G5 + G6（刷新后必跑）
    python tools/verify_ledger.py --docs     # 加 G7（pre-commit / 文档变更后）
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
from kb import config, hashing  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
SKIP_REASONS = ("no_text_layer", "garbled", "converted_only", "duplicate_of", "no_question")


def base_stem(stem: str) -> str:
    """剥 `<stem>_<hash8>` / `<stem>__<hash8>` 同名冲突后缀（build_db._base_stem 同款）。"""
    for cut in (9, 10):          # `_a1b2c3d4` 9 字符；`__a1b2c3d4` 10 字符
        if len(stem) > cut and stem[-cut] == "_" and \
                all(c in "0123456789abcdef" for c in stem[-cut + 1:]):
            return stem[:-cut]
    return stem


def mirror_hash_of(md_rel: str) -> str:
    """md 镜像行的 doc_hash 口径：sha256("md-mirror:"+rel)[:16]（extract/build_db 同款）。"""
    return hashlib.sha256(("md-mirror:" + md_rel).encode("utf-8")).hexdigest()[:16]


def load_skip_log() -> dict[str, str]:
    """meta/skip_log.json → {rel: reason}。rel 可填源相对路径或 md 相对路径（均 posix）。"""
    f = config.META_DIR / "skip_log.json"
    if not f.exists():
        return {}
    data = json.loads(f.read_text(encoding="utf-8"))
    return {x["rel"].replace("\\", "/"): x.get("reason", "") for x in data.get("items", [])}


def _gate(gid: str, name: str, level: str, state: str, fails: list, detail: dict, sql: str) -> dict:
    """门禁结论必须带 SQL 与口径（docs/15 §2.1-5）。"""
    return {"id": gid, "name": name, "level": level, "state": state,
            "fails": fails, "detail": detail, "sql": sql}


# ---------------- G1 源-库全覆盖 ----------------
def g1_coverage(kb_conn, out_conn, skip: dict[str, str]) -> dict:
    """classified 每篇：doc_contents 有行 且 有题；否则须在 skip_log 登记原因。

    md 镜像关联口径：源 file_name stem 剥 hash 后缀 ↔ doc_contents.md_rel stem 剥后缀
    （build_db 的 by_stem 同款；同名多源取首个是已知局限，报告 detail 注明）。
    """
    sql = ("SELECT doc_hash,file_name,dir_path,src_path FROM source_documents WHERE status='classified'; "
           "-- 有题判定: questions.doc_hash ∈ {源doc_hash} ∪ {md镜像mirror_hash}")
    rows = kb_conn.execute(
        "SELECT doc_hash, file_name, dir_path, src_path, category, ext "
        "FROM source_documents WHERE status='classified'").fetchall()
    dc_doc = {r[0] for r in out_conn.execute(
        "SELECT DISTINCT doc_hash FROM doc_contents WHERE doc_hash IS NOT NULL")}
    q_docs = {r[0] for r in out_conn.execute("SELECT DISTINCT doc_hash FROM questions")}
    md_by_stem: dict[str, str] = {}
    for (rel,) in out_conn.execute("SELECT DISTINCT md_rel FROM doc_contents"):
        md_by_stem.setdefault(base_stem(Path(rel).stem), rel)

    fails, n_skip = [], 0
    for r in rows:
        stem = base_stem(Path(r["file_name"]).stem)
        md_rel = md_by_stem.get(stem)
        mirror = mirror_hash_of(md_rel) if md_rel else None
        has_doc = (r["doc_hash"] in dc_doc) or (md_rel is not None)
        has_q = (r["doc_hash"] in q_docs) or (mirror in q_docs if mirror else False)
        if has_doc and has_q:
            continue
        rel = f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"]
        if rel in skip:
            n_skip += 1
            continue
        fails.append({"rel": rel, "file_name": r["file_name"], "ext": r["ext"],
                      "category": r["category"],
                      "has_doc_contents": "Y" if has_doc else "N",
                      "has_questions": "Y" if has_q else "N",
                      "suggest": "backfill_convert --convert" if not has_doc else "extract 重跑"})
    state = "PASS" if not fails else "FAIL"
    return _gate("G1", "源-库全覆盖", "硬", state, fails,
                 {"classified": len(rows), "skipped_by_log": n_skip,
                  "note": "md 镜像按 stem 关联，同名多源取首个（build_db by_stem 同款局限）"}, sql)


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
        if rel in skip:
            continue
        fails.append({"rel": rel, "kind": "md_not_in_db", "suggest": "build_db 重建"})

    # ② ocr_text 有全文却无 md（350道 事故回归样本）
    if ocr_root.exists():
        md_stems = {base_stem(p.stem) for p in md_root.rglob("*.md")} if md_root.exists() else set()
        for txt in sorted(ocr_root.rglob("*.txt")):
            src_rel = txt.relative_to(ocr_root).as_posix()
            if src_rel.endswith(".txt"):
                src_rel = src_rel[:-4]
            if base_stem(Path(src_rel).stem) in md_stems:
                continue
            if src_rel in skip or Path(src_rel).name in skip:
                continue
            fails.append({"rel": src_rel, "kind": "ocr_text_no_md",
                          "suggest": "backfill_convert --merge-ocr / ocr_merge"})

    # ③ doc_contents 有行却 0 题且未标 no_question
    for md_rel, doc_hash, mirror in out_conn.execute(
            "SELECT md_rel, doc_hash, mirror_hash FROM doc_contents"):
        n = out_conn.execute(
            "SELECT COUNT(*) FROM questions WHERE doc_hash IN (?, ?)",
            (doc_hash, mirror)).fetchone()[0]
        if n == 0 and skip.get(md_rel) != "no_question":
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
    except Exception:  # noqa: BLE001 —— vec0 扩展未加载（sqlite_vec 不可用）时无法校验
        return _gate("G4", "索引一致性", "硬", "WARN", [],
                     {"questions": n_q, "note": "vec_questions 是 vec0 虚表且 sqlite_vec 扩展不可加载，跳过校验"},
                     sql)
    q_ids = {r[0] for r in out_conn.execute("SELECT qid FROM questions")}
    n_fts = out_conn.execute("SELECT COUNT(*) FROM questions_fts").fetchone()[0]
    fails = []
    if n_vec != n_q or vec_ids != q_ids:
        fails.append({"kind": "vec_mismatch", "questions": n_q, "vec": n_vec,
                      "qid_diff": len(q_ids ^ vec_ids)})
    if n_fts != n_q:
        fails.append({"kind": "fts_mismatch", "questions": n_q, "fts": n_fts})
    state = "PASS" if not fails else "FAIL"
    return _gate("G4", "索引一致性", "硬", state, fails,
                 {"questions": n_q, "vec": n_vec, "fts": n_fts}, sql)


# ---------------- G5 台账-磁盘一致 ----------------
def g5_disk(kb_conn, corpus_root: Path, build_dir: Path) -> dict:
    """classified 在盘；superseded 在 99 区或已删且留痕；99 区每件在台账。"""
    sql = ("classified: src_path exists; superseded: dir_path LIKE '99-%' OR 留痕(note/retire_log); "
           "99区: sha256(file)[:16] ∈ source_documents.doc_hash")
    fails = []
    retire_names = set()
    retire_log = build_dir / "retire_log.csv"
    if retire_log.exists():
        with retire_log.open(encoding="utf-8-sig", newline="") as f:
            for row in csv.reader(f):
                if len(row) >= 3 and row[2].strip():
                    retire_names.add(Path(row[2].strip()).name)

    for r in kb_conn.execute(
            "SELECT doc_hash, file_name, dir_path, src_path, status, note "
            "FROM source_documents WHERE status IN ('classified','superseded')").fetchall():
        src = Path(r["src_path"] or "")
        if r["status"] == "classified":
            if not src.exists():
                fails.append({"kind": "classified_src_missing", "rel": r["file_name"]})
            continue
        # superseded：在盘则须在 99 区，或 note 有留痕（converted_to 原地转换 /
        # review_cli 退役确认——"已退役待物理清理"是合法中间态，docs/15 G5 意图=有记录即过）；
        # 不在盘则同样须有留痕（note / retire_log）
        if src.exists():
            in_99 = (r["dir_path"] or "").startswith(config.QUARANTINE_PREFIX) \
                    or config.QUARANTINE_PREFIX in str(src)
            traced = bool((r["note"] or "").strip())
            if not (in_99 or traced):
                fails.append({"kind": "superseded_not_quarantined", "rel": r["file_name"]})
        elif not ((r["note"] or "").strip() or r["file_name"] in retire_names):
            fails.append({"kind": "superseded_no_trace", "rel": r["file_name"]})

    q_dir = corpus_root / config.QUARANTINE_PREFIX
    if q_dir.exists():
        known = {r[0] for r in kb_conn.execute("SELECT doc_hash FROM source_documents")}
        for p in q_dir.rglob("*"):
            if not p.is_file():
                continue
            if p.name in retire_names:
                continue          # 退役 md 镜像（retire_log 留痕；台账只记源文档，不登记派生件）
            if hashing.sha256_file(p)[:16] not in known:
                fails.append({"kind": "orphan_in_99", "rel": str(p.relative_to(corpus_root))})
    state = "PASS" if not fails else "FAIL"
    return _gate("G5", "台账-磁盘一致", "硬", state, fails,
                 {"retire_log_rows": len(retire_names)}, sql)


# ---------------- G6 打标质量（未启用） ----------------
def g6_label() -> dict:
    return _gate("G6", "打标质量", "软→硬", "SKIP", [],
                 {"note": "labels_staging 未建（线上打标未启用），打标后转硬"}, "")


# ---------------- G7 文档一致性（软） ----------------
def g7_docs(repo_root: Path, actual_tests: int) -> dict:
    """README 测试数 vs 实际；尚未实现清单不得列已存在脚本；docs 编号范围 vs 实际。"""
    warns = []
    readme_p = repo_root / "README.md"
    readme = readme_p.read_text(encoding="utf-8") if readme_p.exists() else ""
    m = re.search(r"tests/[^\n]*?(\d+)\s*个用例", readme)
    if m and int(m.group(1)) != actual_tests:
        warns.append({"kind": "tests_count_stale",
                      "readme": int(m.group(1)), "actual": actual_tests})
    # 「尚未实现」节的列表项不得列 tools/ 已存在的脚本（`- ~~` 删除线=已落地条目，跳过；
    # 正文括号里的脚本引用不在行首列表项上，不构成"声称未实现"）
    sec = re.search(r"## 尚未实现.*?(?=\n## |\Z)", readme, re.S)
    if sec:
        for line in sec.group(0).splitlines():
            s = line.strip()
            if not s.startswith("-") or s.startswith("- ~~"):
                continue
            m = re.search(r"`([\w/]+\.py)`", s)
            if m and (repo_root / "tools" / m.group(1)).exists():
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


def write_report(build_dir: Path, gates: list[dict], elapsed: float) -> tuple[Path, Path]:
    """verify_report.json / .md 落盘。"""
    hard_fail = [g for g in gates if g["level"] == "硬" and g["state"] == "FAIL"]
    summary = {
        "at": datetime.now().strftime(NOW_FMT),
        "elapsed_sec": round(elapsed, 1),
        "hard_fail": len(hard_fail),
        "verdict": "FAIL" if hard_fail else "PASS",
        "gates": {g["id"]: g["state"] for g in gates},
    }
    js = build_dir / "verify_report.json"
    js.write_text(json.dumps({"summary": summary, "gates_detail": gates},
                             ensure_ascii=False, indent=1), encoding="utf-8")

    lines = [f"# 核账门禁报告（{summary['at']}）", "",
             f"- 结论：**{summary['verdict']}**（硬门禁 FAIL {len(hard_fail)} 道，耗时 {summary['elapsed_sec']}s）",
             f"- 口径：G1-G5 刷新后必跑；G6 打标后；G7 pre-commit（--docs）", ""]
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
    """G1/G2 FAIL 项 → build/backfill_plan.csv（门禁即清单，docs/15 §2.1-3）。"""
    plan = []
    for g in gates:
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
    ap = argparse.ArgumentParser(description="G1-G7 核账门禁（任一硬门禁 FAIL 即非 0 退出）")
    ap.add_argument("--docs", action="store_true", help="加跑 G7 文档一致性（pre-commit 用）")
    ap.add_argument("--kb-db", default=str(config.DB_PATH), help="台账 kb.sqlite")
    ap.add_argument("--out-db", default=str(Path(config.BUILD_DIR) / "面经库.db"),
                    help="检索库 面经库.db")
    args = ap.parse_args(argv)

    import sqlite3
    started = time.perf_counter()
    kb_conn = sqlite3.connect(f"file:{Path(args.kb_db).as_posix()}?mode=ro", uri=True)
    kb_conn.row_factory = sqlite3.Row
    out_conn = sqlite3.connect(f"file:{Path(args.out_db).as_posix()}?mode=ro", uri=True)
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
        g6_label(),
    ]
    if args.docs:
        repo_root = Path(__file__).resolve().parents[1]
        gates.append(g7_docs(repo_root, _collect_tests(repo_root)))
    kb_conn.close()
    out_conn.close()

    js, md = write_report(build_dir, gates, time.perf_counter() - started)
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
