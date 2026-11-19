# -*- coding: utf-8 -*-
"""5,000 题小排量预生产试跑：一份配置即可准备/执行（P1）。

边界（docs/35）：
- 生产 `kb.sqlite` 只读，所有写入只落 `<pilot_root>/build`、`<pilot_root>/corpus`；
- 按 `target_min/target_max` 对候选文档做无写入抽取估算，累计到目标题数（同一物理文件只选一次）；
- `run` 依次执行 extract → canonical → refresh → 检索冒烟；打标只在配置了 key 时可选执行。

用法：
    python -m mianjing_kb.pilot5000 prepare --config D:\\pilot\\5000\\config.json
    python -m mianjing_kb.pilot5000 run --config D:\\pilot\\5000\\config.json
    python -m mianjing_kb.pilot5000 label --config D:\\pilot\\5000\\config.json
    python -m mianjing_kb.pilot5000 report --config D:\\pilot\\5000\\config.json
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

NOW_FMT = "%Y-%m-%dT%H:%M:%S"

DEFAULT_CONFIG = {
    # 仓库内只放占位/环境变量；真实路径一律由 --config 或环境变量提供
    "prod_db": os.environ.get("MIANJING_PROD_DB", ""),
    "prod_md_dir": os.environ.get("MIANJING_PROD_MD_DIR", ""),
    "pilot_root": os.environ.get("PILOT_ROOT", ""),
    "python": sys.executable,
    "repo_root": str(Path(__file__).resolve().parents[1]),
    "docs": 300,
    "seed": 7,
    "target_min": 4500,
    "target_max": 5500,
    "llm": {
        "provider": "online",
        "base": "https://api.qnaigc.com/v1",
        "model": "deepseek-v4.1-flash-1017",
        "key": "",
        "concurrency": 1,
        "price": "1,4",
        "thinking": "disabled",
        "rpm": 5,
    },
}

DELETED_TABLES = (
    "question_variants", "canonical_dependents", "merge_candidates",
    "canonical_questions", "questions", "labels_staging", "label_facts",
    "golden_labels", "document_events",
)


def _now() -> str:
    return datetime.now().strftime(NOW_FMT)


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def ensure_config(path: Path, overrides: dict | None = None) -> dict:
    if not path.exists():
        cfg = {**DEFAULT_CONFIG, "llm": dict(DEFAULT_CONFIG["llm"])}
        for key, value in (overrides or {}).items():
            if value not in (None, ""):
                cfg[key] = value
        _write_json(path, cfg)
        print(f"[pilot] 已生成配置：{path}；请只填 llm.key 后再 run")
    return json.loads(path.read_text(encoding="utf-8"))


def _backup(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        raise RuntimeError(f"目标已存在，拒绝覆盖：{dst}")
    s = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    d = sqlite3.connect(str(dst))
    try:
        s.backup(d)
        d.commit()
    finally:
        d.close()
        s.close()
    con = sqlite3.connect(f"file:{dst.as_posix()}?mode=ro", uri=True)
    try:
        state = con.execute("PRAGMA quick_check").fetchone()[0]
    finally:
        con.close()
    if state != "ok":
        raise RuntimeError(f"副本 quick_check={state}，停止")


def _doc_path(prod: sqlite3.Connection, doc_hash: str, prod_md: Path) -> Path | None:
    row = prod.execute(
        "SELECT md_rel FROM document_products WHERE doc_hash=?", (doc_hash,)).fetchone()
    md_rel = (row[0] or "").strip() if row else ""
    if md_rel:
        p = _safe_join(prod_md, md_rel)
        if p and p.is_file():
            return p
    row = prod.execute(
        "SELECT src_path FROM source_documents WHERE doc_hash=?", (doc_hash,)).fetchone()
    if row and row[0]:
        p = Path(row[0])
        if p.is_file():
            return p
    return None


def _sample_docs(prod: sqlite3.Connection, docs: int, seed: int,
                 target_min: int, target_max: int, prod_md: Path) -> list[tuple[str, int]]:
    """按实际抽取产出累计到目标题数，避免共享 MD 被重复计入。

    生产 `questions_n` 是去重后的行数，直接按它抽样会在副本里抽到“同一 MD 的
    多个身份”，重抽后题数翻倍。这里对候选文档先做一次无写入的抽取估算，再按
    目标题数截断；同一物理文件只选一次。返回 [(doc_hash, 估算题数)]。
    """
    from mianjing_kb.extract import extract_doc_status

    rows = prod.execute(
        "SELECT q.doc_hash, COUNT(*) AS n, MIN(s.gate_result) AS gate "
        "FROM questions q LEFT JOIN source_documents s ON s.doc_hash=q.doc_hash "
        "GROUP BY q.doc_hash ORDER BY q.doc_hash").fetchall()
    if docs > len(rows):
        raise RuntimeError(f"可选文档 {len(rows)} < 请求 {docs}")
    candidates = random.Random(seed).sample(rows, len(rows))
    selected: list[tuple[str, int]] = []
    estimated = 0
    used_paths: set[str] = set()
    for r in candidates:
        doc_hash = str(r["doc_hash"])
        if (r["gate"] or "") == "non_content":
            continue
        path = _doc_path(prod, doc_hash, prod_md)
        if path is None:
            continue
        key = str(path.resolve()).lower()
        if key in used_paths:
            continue
        try:
            extracted, _status = extract_doc_status(doc_hash, path)
        except Exception:  # noqa: BLE001
            continue
        n = len(extracted)
        if n <= 0:
            continue
        if estimated + n > target_max:
            continue
        selected.append((doc_hash, n))
        used_paths.add(key)
        estimated += n
        if len(selected) >= docs or estimated >= target_min:
            break
    if not selected:
        raise RuntimeError("没有可抽取的候选文档")
    if estimated < target_min:
        raise RuntimeError(
            f"实际可抽 {estimated} 题，低于目标下限 {target_min}；增大 docs 后重试")
    return selected


def _safe_join(root: Path, rel: str) -> Path | None:
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return None
    p = (root / rel).resolve()
    try:
        p.relative_to(root.resolve())
    except ValueError:
        return None
    return p


def prepare(cfg: dict) -> dict:
    prod_db = Path(cfg["prod_db"])
    prod_md = Path(cfg["prod_md_dir"])
    root = Path(cfg["pilot_root"])
    build = root / "build"
    corpus_md = root / "corpus" / "20-题库MD"
    pilot_db = build / "kb.sqlite"
    if not prod_db.exists():
        raise RuntimeError(f"生产库不存在：{prod_db}")
    if not prod_md.exists():
        raise RuntimeError(f"生产 MD 目录不存在：{prod_md}")
    if pilot_db.exists():
        raise RuntimeError(f"试跑库已存在，防止覆盖：{pilot_db}；先移走或删除 {root}")

    build.mkdir(parents=True, exist_ok=True)
    corpus_md.mkdir(parents=True, exist_ok=True)
    prod = sqlite3.connect(f"file:{prod_db.as_posix()}?mode=ro", uri=True)
    prod.row_factory = sqlite3.Row
    try:
        sample = _sample_docs(prod, int(cfg["docs"]), int(cfg["seed"]),
                              int(cfg["target_min"]), int(cfg["target_max"]), prod_md)
        total = sum(n for _h, n in sample)
        if not (int(cfg["target_min"]) <= total <= int(cfg["target_max"])):
            raise RuntimeError(
                f"抽样 {len(sample)} 文档共 {total} 题，超出 "
                f"{cfg['target_min']}–{cfg['target_max']}；调整 docs 或 seed 后重试")
        _backup(prod_db, pilot_db)
    finally:
        prod.close()

    con = sqlite3.connect(str(pilot_db))
    try:
        con.execute("PRAGMA foreign_keys=OFF")
        con.execute("CREATE TABLE pilot_docs(doc_hash TEXT PRIMARY KEY)")
        con.executemany("INSERT INTO pilot_docs VALUES(?)", [(h,) for h, _n in sample])
        for table in DELETED_TABLES:
            con.execute(f"DELETE FROM {table}")
        con.execute("DELETE FROM document_products WHERE doc_hash NOT IN "
                    "(SELECT doc_hash FROM pilot_docs)")
        con.execute("DELETE FROM source_documents WHERE doc_hash NOT IN "
                    "(SELECT doc_hash FROM pilot_docs)")
        con.execute("UPDATE document_products SET extracted_at=NULL, indexed_at=NULL, "
                    "questions_n=0")
        con.commit()
        con.execute("PRAGMA foreign_keys=ON")
        state = con.execute("PRAGMA quick_check").fetchone()[0]
        products = con.execute(
            "SELECT doc_hash, md_rel FROM document_products "
            "WHERE md_rel IS NOT NULL AND md_rel<>''").fetchall()
    finally:
        con.close()
    if state != "ok":
        raise RuntimeError(f"裁剪副本 quick_check={state}，停止")

    copied = missing = 0
    for _hash, md_rel in products:
        src = _safe_join(prod_md, md_rel)
        if src is None or not src.exists() or not src.is_file():
            missing += 1
            continue
        dst = corpus_md / (md_rel or "").replace("\\", "/").lstrip("/")
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        copied += 1

    with (build / "pilot_docs.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["doc_hash", "questions"])
        writer.writerows(sample)
    plan = {
        "generated_at": _now(),
        "prod_db": str(prod_db),
        "pilot_root": str(root),
        "pilot_db": str(pilot_db),
        "docs": len(sample),
        "target_questions": total,
        "target_range": [int(cfg["target_min"]), int(cfg["target_max"])],
        "seed": int(cfg["seed"]),
        "md_copied": copied,
        "md_missing": missing,
        "quick_check": state,
    }
    _write_json(build / "pilot_plan.json", plan)
    return plan


def _llm_key(cfg: dict) -> str:
    """优先 config.llm.key；其次用户环境变量 LLM_KEY（key 不进仓库）。"""
    return str((cfg.get("llm") or {}).get("key") or os.environ.get("LLM_KEY") or "")


def _env(cfg: dict) -> dict:
    root = Path(cfg["pilot_root"])
    env = os.environ.copy()
    env.update({
        "KB_CORPUS_ROOT": str(root / "corpus"),
        "KB_BUILD_DIR": str(root / "build"),
        "KB_DB": str(root / "build" / "kb.sqlite"),
        "KB_META_DIR": str(Path(cfg["repo_root"]) / "meta"),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    llm = cfg.get("llm") or {}
    key = _llm_key(cfg)
    if key:
        env.update({
            "LLM_PROVIDER": str(llm.get("provider") or "online"),
            "LLM_BASE": str(llm.get("base") or ""),
            "LLM_MODEL": str(llm.get("model") or ""),
            "LLM_KEY": key,
            "LLM_CONCURRENCY": str(llm.get("concurrency") or 1),
            "LLM_PRICE": str(llm.get("price") or "0,0"),
            "LLM_THINKING": str(llm.get("thinking") or ""),
            "LLM_RPM": str(llm.get("rpm") or ""),
        })
    return env


def _run(cmd: list[str], env: dict, log: Path, cwd: Path) -> dict:
    started = datetime.now()
    with log.open("a", encoding="utf-8") as f:
        f.write(f"\n$ {' '.join(cmd)}\n")
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdout=f,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8")
    return {"cmd": cmd, "rc": proc.returncode, "seconds": round(
        (datetime.now() - started).total_seconds(), 1)}


def run(cfg: dict, with_label: bool = False) -> dict:
    root = Path(cfg["pilot_root"])
    db = root / "build" / "kb.sqlite"
    if not db.exists():
        raise RuntimeError(f"试跑库不存在，先执行 prepare：{db}")
    env = _env(cfg)
    log = root / "build" / "pilot_run.log"
    py = cfg["python"]
    plan_path = root / "build" / "pilot_plan.json"
    docs = str(int(cfg["docs"]))
    if plan_path.exists():
        docs = str(int(json.loads(plan_path.read_text(encoding="utf-8"))["docs"]))
    steps = [
        ("extract", [py, "-m", "mianjing_kb.cli", "extract", "--db", str(db), "--limit", docs]),
        ("canonical", [py, "-m", "mianjing_kb.cli", "canonical", "--db", str(db)]),
        ("refresh", [py, "-m", "mianjing_kb.cli", "refresh", "--reembed"]),
        ("search", [py, "-m", "mianjing_kb.cli", "search", "HashMap 扩容机制", "--limit", "5"]),
        ("eval_demo", [py, "-m", "mianjing_kb.cli", "eval", "--queries",
                       str(Path(cfg["repo_root"]) / "eval" / "demo_queries.yaml"),
                       "--db", str(root / "build" / "面经库.db"), "--k", "5", "--mode", "kw"]),
        ("eval_golden", [py, "-m", "mianjing_kb.cli", "eval", "--golden",
                         str(Path(cfg["repo_root"]) / "eval" / "golden_queries.yaml"),
                         "--db", str(root / "build" / "面经库.db"), "--k", "10", "--mode", "kw"]),
    ]
    if with_label:
        key = _llm_key(cfg)
        if not key:
            raise RuntimeError("with_label 需要先填配置 llm.key 或设置环境变量 LLM_KEY")
        steps += [
            ("label_check", [py, "-m", "mianjing_kb.cli", "label", "--check"]),
            ("label_dry_run", [py, "-m", "mianjing_kb.cli", "label", "--dry-run",
                               "--limit", "100"]),
        ]
    results = []
    for name, cmd in steps:
        out = _run(cmd, env, log, Path(cfg["repo_root"]))
        out["stage"] = name
        results.append(out)
        if out["rc"] != 0:
            break
    summary = {"generated_at": _now(), "stages": results,
               "stopped_on_failure": bool(results and results[-1]["rc"] != 0)}
    _write_json(root / "build" / "pilot_stages.json", summary)
    return summary


def label(cfg: dict) -> dict:
    """只跑 100 题打标冒烟；填好 llm.key 后可单独执行，不重跑核心链路。"""
    root = Path(cfg["pilot_root"])
    db = root / "build" / "kb.sqlite"
    if not db.exists():
        raise RuntimeError(f"试跑库不存在，先执行 prepare：{db}")
    if not _llm_key(cfg):
        raise RuntimeError("label 需要先填配置 llm.key 或设置环境变量 LLM_KEY")
    env = _env(cfg)
    log = root / "build" / "pilot_run.log"
    py = cfg["python"]
    steps = [
        ("label_check", [py, "-m", "mianjing_kb.cli", "label", "--check"]),
        ("label_dry_run", [py, "-m", "mianjing_kb.cli", "label", "--dry-run",
                           "--limit", "100"]),
    ]
    results = []
    for name, cmd in steps:
        out = _run(cmd, env, log, Path(cfg["repo_root"]))
        out["stage"] = name
        results.append(out)
        if out["rc"] != 0:
            break
    summary = {"generated_at": _now(), "stages": results,
               "stopped_on_failure": bool(results and results[-1]["rc"] != 0)}
    _write_json(root / "build" / "pilot_label.json", summary)
    return summary


def report(cfg: dict) -> dict:
    root = Path(cfg["pilot_root"])
    db = root / "build" / "kb.sqlite"
    if not db.exists():
        raise RuntimeError(f"试跑库不存在：{db}")
    prod_schema = None
    prod_db = Path(cfg["prod_db"])
    if prod_db.exists():
        pcon = sqlite3.connect(f"file:{prod_db.as_posix()}?mode=ro", uri=True)
        try:
            prod_schema = pcon.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        finally:
            pcon.close()
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        q = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        qkey_dup = con.execute(
            "SELECT COUNT(*) FROM (SELECT q_key FROM questions GROUP BY q_key "
            "HAVING COUNT(*)>1)").fetchone()[0]
        orphan_v = con.execute(
            "SELECT COUNT(*) FROM question_variants WHERE q_key NOT IN "
            "(SELECT q_key FROM questions)").fetchone()[0]
        orphan_c = con.execute(
            "SELECT COUNT(*) FROM canonical_dependents WHERE q_key NOT IN "
            "(SELECT q_key FROM questions)").fetchone()[0]
        docs = con.execute("SELECT COUNT(DISTINCT doc_hash) FROM questions").fetchone()[0]
        out_db = root / "build" / "面经库.db"
        vec = None
        if out_db.exists():
            vcon = sqlite3.connect(f"file:{out_db.as_posix()}?mode=ro", uri=True)
            try:
                import sqlite_vec
                vcon.enable_load_extension(True)
                sqlite_vec.load(vcon)
                vcon.enable_load_extension(False)
                vec = vcon.execute("SELECT COUNT(*) FROM vec_questions").fetchone()[0]
            except (sqlite3.Error, ImportError):
                vec = None
            finally:
                vcon.close()
        data = {
            "generated_at": _now(),
            "prod_schema_version": prod_schema,
            "pilot_schema_version": con.execute(
                "SELECT MAX(version) FROM schema_version").fetchone()[0],
            "sample": {"docs": docs, "actual_questions": q},
            "integrity": {"quick_check": con.execute("PRAGMA quick_check").fetchone()[0],
                          "qkey_dup": qkey_dup, "orphan_variants": orphan_v,
                          "orphan_canonical": orphan_c},
            "vectors": vec,
        }
    finally:
        con.close()
    _write_json(root / "build" / "pilot_5000_report.json", data)
    return data


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="5,000 题小排量预生产试跑")
    ap.add_argument("stage", choices=["prepare", "run", "label", "report"])
    ap.add_argument("--config", default=r"D:\pilot\5000\config.json")
    ap.add_argument("--with-label", action="store_true", help="run 时额外跑 100 题打标冒烟")
    args = ap.parse_args(argv)
    path = Path(args.config)
    cfg = ensure_config(path)
    try:
        if args.stage == "prepare":
            out = prepare(cfg)
        elif args.stage == "run":
            out = run(cfg, with_label=args.with_label)
        elif args.stage == "label":
            out = label(cfg)
        else:
            out = report(cfg)
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": f"{type(ex).__name__}: {ex}"},
                         ensure_ascii=False, indent=1))
        return 2
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
