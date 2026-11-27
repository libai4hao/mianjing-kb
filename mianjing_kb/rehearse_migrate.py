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
清理前先证明 `--work` 是预演专用目录（写入 `.kb-rehearsal` 标记，且只含 kb_dry/build 等产物）：
与源台账目录重叠、或混入无关文件时直接拒绝运行，不会顺手删源库或用户文件。
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
MARKER = ".kb-rehearsal"
# 预演目录里允许出现的东西（清理只对这类目录负责）
WORK_OUTPUTS = {MARKER, "kb_dry.sqlite", "kb_dry.sqlite-wal", "kb_dry.sqlite-shm", "build"}


def _work_guard(src: Path, work: Path) -> None:
    """清理前先证明 work 是"预演专用目录"，不是用户目录或源台账所在目录。

    `shutil.rmtree(work)` 是全删：work 指到源库父目录、上级目录或混进别的东西时，
    旧实现会把源台账和无关文件一起删掉。这里 fail-closed，宁可拒绝跑也不猜。
    """
    src_r = Path(src).resolve()
    work_r = Path(work).resolve()
    if work_r == src_r or work_r in src_r.parents or src_r in work_r.parents:
        raise ValueError(f"--work 与源台账目录重叠，拒绝清理：{work_r}")
    if not work_r.exists():
        return
    if not work_r.is_dir():
        raise ValueError(f"--work 不是目录：{work_r}")
    unexpected = sorted(p.name for p in work_r.iterdir()
                        if p.name not in WORK_OUTPUTS and not p.name.startswith("md_identity_report"))
    if unexpected:
        raise ValueError(
            f"--work 目录含非预演产物，拒绝复用/清理：{work_r} :: {', '.join(unexpected[:5])}")


def _copy_db(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.unlink(missing_ok=True)
    src_con = sqlite3.connect(db.ro_uri(src), uri=True)
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
    _work_guard(src, work)
    work.mkdir(parents=True, exist_ok=True)
    (work / MARKER).write_text("mianjing-kb migration rehearsal\n", encoding="utf-8")
    dry = work / "kb_dry.sqlite"
    _copy_db(src, dry)

    before_conn = sqlite3.connect(db.ro_uri(dry), uri=True)
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
    cleanup = "kept"
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
            # 删除是真的发生了才算"已删除"：rmtree(ignore_errors=True) 可能静默失败
            cleanup = "removed" if not work.exists() else "failed"

    ok = (not plan.get("skipped")) and plan.get("exit") == 0
    if plan.get("skipped"):
        ok = True
    if cleanup == "failed":
        ok = False
    return {
        "ok": ok,
        "src": str(src),
        "dry_db": str(dry) if keep else ("(已删除)" if cleanup == "removed" else str(dry)),
        "before": before,
        "after": after,
        "new_migrations": [v for v in applied if v not in set(before["migrations"])],
        "plan": plan,
        "kept": keep,
        "cleanup": cleanup,
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
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
