# -*- coding: utf-8 -*-
"""kb demo：用仓库自带的小型合成语料跑通「接入 → 分类 → 切题 → 建库 → 门禁 → 检索」。

设计要点：
- 全程在临时目录（或 --work 指定目录）里跑，绝不碰真实语料 / 生产库；
- 每一步都用子进程调 `mianjing_kb/<step>.py`，环境变量隔离，避免同一进程里 config 常量被上一次运行污染；
- 合成语料在 examples/demo_corpus（5 个源文件 + 5 个 md 镜像 + 1 份重复件），无任何真实面经内容。

用法：
    python -m mianjing_kb.demo                 # 临时目录跑完即删（只打印统计）
    python -m mianjing_kb.demo --keep          # 保留临时目录，打印路径供手工检查
    python -m mianjing_kb.demo --work <dir>    # 指定工作目录（总是保留）
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(REPO))
from mianjing_kb.kb import status  # noqa: E402

DEMO_SRC = REPO / "examples" / "demo_corpus"
TOOLS = REPO / "mianjing_kb"


def _run(step: str, args: list[str], env: dict) -> tuple[int, dict]:
    """跑一步子进程，返回 (exit_code, 解析到的 JSON 摘要)。"""
    p = subprocess.run(
        [sys.executable, str(TOOLS / f"{step}.py"), *args],
        capture_output=True, text=True, encoding="utf-8", env=env, cwd=str(REPO),
    )
    out = p.stdout.strip()
    summary = {}
    if out:
        try:
            summary = json.loads(out[out.index("{"):]) if "{" in out else {}
        except ValueError:
            summary = {}
    if p.returncode != 0:
        sys.stderr.write(f"[demo] {step} 失败（exit={p.returncode}）\n{p.stdout}\n{p.stderr}\n")
    return p.returncode, summary


def prepare(work: Path) -> Path:
    corpus = work / "corpus"
    if corpus.exists():
        shutil.rmtree(corpus)
    shutil.copytree(DEMO_SRC, corpus)
    build = work / "build"
    if build.exists():                       # 复用 --work 时清掉上次的库与日志，保证结果可重复
        shutil.rmtree(build)
    build.mkdir(parents=True)
    return corpus


def run(work: Path, *, quiet: bool = False) -> dict:
    corpus = prepare(work)
    build = work / "build"
    db_path = build / "kb.sqlite"
    env = dict(os.environ)
    env.update({
        "KB_CORPUS_ROOT": str(corpus),
        "KB_BUILD_DIR": str(build),
        "KB_DB": str(db_path),
    })

    result: dict = {"work": str(work), "corpus": str(corpus), "db": str(db_path), "steps": {}}
    steps = [
        ("intake", []),
        ("classify", []),
        ("extract", []),
        ("build_db", []),
        ("verify_ledger", ["--kb-db", str(db_path)]),
    ]
    for step, args in steps:
        code, summary = _run(step, args, env)
        result["steps"][step] = {"exit": code, "summary": summary}
        if not quiet:
            print(f"[demo] {step:13s} exit={code} {json.dumps(summary, ensure_ascii=False)[:200]}")
        if code != 0 and step != "verify_ledger":
            result["ok"] = False
            return result

    gates = result["steps"]["verify_ledger"]["summary"].get("gates", {})
    code, hits = _run("search", ["HashMap", "--mode", "kw", "--limit", "3"], env)
    result["steps"]["search"] = {"exit": code, "hits": hits.get("hits", 0)}
    if not quiet:
        print(f"[demo] search        exit={code} hits={hits.get('hits', 0)}")
    result["gates"] = gates
    result["counts"] = _counts(db_path, build / "面经库.db")
    result["ok"] = result["steps"]["verify_ledger"]["exit"] == 0 and code == 0
    return result


def _counts(db_path: Path, out_db: Path) -> dict:
    """从两个库直接读行数（不依赖各脚本的打印格式）。"""
    import sqlite3
    out: dict = {}
    if db_path.exists():
        con = sqlite3.connect(str(db_path))
        out["docs_classified"] = con.execute(
            f"SELECT COUNT(*) FROM source_documents WHERE status='{status.CLASSIFIED}'").fetchone()[0]
        out["docs_ingested"] = con.execute(
            f"SELECT COUNT(*) FROM source_documents WHERE status IN {status.sql_in(status.INGESTED)}").fetchone()[0]
        out["docs_total"] = con.execute("SELECT COUNT(*) FROM source_documents").fetchone()[0]
        out["questions"] = con.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        con.close()
    if out_db.exists():
        con = sqlite3.connect(str(out_db))
        names = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
        if "doc_contents" in names:
            out["doc_contents"] = con.execute("SELECT COUNT(*) FROM doc_contents").fetchone()[0]
        con.close()
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="用合成语料跑通全链路（不碰生产库）")
    ap.add_argument("--work", default=None, help="工作目录（默认临时目录）")
    ap.add_argument("--keep", action="store_true", help="保留临时工作目录（默认跑完删除；--work 指定的目录总是保留）")
    ap.add_argument("--no-keep", action="store_true", help="兼容旧参数：与默认行为一致，跑完删除临时目录")
    args = ap.parse_args(argv)

    temp = not args.work
    work = Path(args.work) if args.work else Path(tempfile.mkdtemp(prefix="kb-demo-"))
    work.mkdir(parents=True, exist_ok=True)
    res = run(work)
    print(json.dumps({"ok": res["ok"], "gates": res.get("gates", {}),
                      "work": res["work"], "db": res["db"]}, ensure_ascii=False, indent=1))
    if temp and not args.keep:
        shutil.rmtree(work, ignore_errors=True)
    return 0 if res["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
