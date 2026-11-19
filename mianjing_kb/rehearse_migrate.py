# -*- coding: utf-8 -*-
"""迁移预演（P1-5）：在 `kb.sqlite` 的副本上跑迁移与抽取计划，产库原文件全程只读。

用法：
    python -m mianjing_kb.rehearse_migrate                     # 默认预演 config.DB_PATH
    python -m mianjing_kb.rehearse_migrate --db <kb.sqlite> --work <dir>
    python -m mianjing_kb.rehearse_migrate --keep              # 保留副本供排查

做四件事：
  1. 用 sqlite backup API 复制一份（不会把 WAL 半写状态带过去）；
  2. 在副本上跑 `db.connect`（触发 001–015 全量迁移）并校验 schema；
  3. 对比迁移前后 `source_documents` 的状态分布与 questions/canonical 行数；
  4. 在副本上跑 `extract --plan`（不读正文、不写库），确认身份映射可解析。

产物：`<work>/kb_dry.sqlite`、`<work>/build/md_identity_report.json`、stdout JSON。
任何一步失败都返回非 0；默认删除副本，`--keep` 保留。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_WORK = Path(config.BUILD_DIR) / "migration_rehearsal"


def _copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    src_con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
        dst_con.commit()
    finally:
        dst_con.close()
        src_con.close()


def _counts(conn: sqlite3.Connection) -> dict:
    out = {
        "source_documents": conn.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0],
        "status": dict(conn.execute(
            "SELECT status, COUNT(*) FROM source_documents GROUP BY 1 ORDER BY 2 DESC").fetchall()),
    }
    for table in ("questions", "canonical_questions", "question_variants", "document_products"):
        try:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except sqlite3.OperationalError:
            out[table] = None       # 迁移前不存在（预期）
    return out


def rehearse(src: Path, work: Path, *, keep: bool = False, run_plan: bool = True) -> dict:
    src = Path(src)
    work = Path(work)
    if not src.exists():
        raise FileNotFoundError(f"台账不存在：{src}")
    dry = work / "kb_dry.sqlite"
    _copy_db(src, dry)

    before_conn = sqlite3.connect(f"file:{dry.as_posix()}?mode=ro", uri=True)
    before_conn.row_factory = sqlite3.Row
    before = _counts(before_conn)
    before["migrations"] = []
    try:
        before["migrations"] = db.applied_versions(before_conn)
    except sqlite3.OperationalError:
        pass                                     # 还没建 schema_version（P1-5 的产库现状）
    before_conn.close()

    applied = []
    after = {}
    plan = {"skipped": True}
    try:
        conn = db.connect(dry)
        try:
            conn.row_factory = sqlite3.Row
            applied = db.applied_versions(conn)
            after = _counts(conn)
        finally:
            conn.close()
        if run_plan:
            env = dict(os.environ)
            build = work / "build"
            build.mkdir(parents=True, exist_ok=True)
            env.update({"KB_DB": str(dry), "KB_BUILD_DIR": str(build),
                        "KB_CORPUS_ROOT": str(config.CORPUS_ROOT)})
            proc = subprocess.run(
                [sys.executable, "-m", "mianjing_kb.extract", "--db", str(dry), "--plan"],
                cwd=str(REPO), env=env, capture_output=True, text=True, timeout=600)
            plan = {"exit": proc.returncode,
                    "stdout": proc.stdout.strip()[-2000:],
                    "stderr": proc.stderr.strip()[-1000:],
                    "report": str(build / "md_identity_report.json")}
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)

    ok = (not plan.get("skipped")) and plan.get("exit") == 0
    if plan.get("skipped"):
        ok = True
    return {
        "ok": ok,
        "src": str(src),
        "dry_db": str(dry) if keep else "(已删除)",
        "before": before,
        "after": after,
        "new_migrations": [v for v in applied if v not in set(before["migrations"])],
        "plan": plan,
        "kept": keep,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="在 kb.sqlite 副本上预演迁移与抽取计划")
    ap.add_argument("--db", default=str(config.DB_PATH), help="源台账（只读）")
    ap.add_argument("--work", default=str(DEFAULT_WORK), help="副本与报告目录")
    ap.add_argument("--keep", action="store_true", help="保留副本与报告（排查用）")
    ap.add_argument("--no-plan", action="store_true", help="只做迁移，不跑 extract --plan")
    args = ap.parse_args(argv)
    try:
        out = rehearse(Path(args.db), Path(args.work), keep=args.keep,
                       run_plan=not args.no_plan)
    except Exception as ex:  # noqa: BLE001
        print(json.dumps({"ok": False, "why": f"{type(ex).__name__}: {ex}"},
                         ensure_ascii=False, indent=1))
        return 1
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0 if out["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
