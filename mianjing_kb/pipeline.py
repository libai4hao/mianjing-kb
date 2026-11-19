# -*- coding: utf-8 -*-
"""pipeline：新增文件的一条命令（去重 → 分类 → 入清单）。

对应 docs/03 §10.4 的"先分类再处理"入口：
    intake（算 hash / 判重 / 写表） → classify（规则判类） → queue（导出可入库清单）

转换/OCR/抽题/建库/refresh 不在本链（见 docs/04 SOP），本命令只管"新文件进台账"。

用法：
    python -m mianjing_kb.pipeline --check                       # 只预览：不写库，只报会新增/重复多少
    python -m mianjing_kb.pipeline                               # 处理 31-收件箱
    python -m mianjing_kb.pipeline --root "<DOWNLOAD_DIR>\\新增"     # 处理任意目录后入库（示例路径，按本机实际替换）
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, status  # noqa: E402

TOOLS = Path(__file__).resolve().parent


def run(args: list[str]) -> dict | None:
    proc = subprocess.run([sys.executable, *args], capture_output=True, text=True, encoding="utf-8")
    if proc.returncode != 0:
        print(f"[fail] {' '.join(args)}\n{proc.stderr[-2000:]}", file=sys.stderr)
        return None
    out = proc.stdout
    try:
        start = out.index("{")
        return json.loads(out[start:])
    except (ValueError, json.JSONDecodeError):
        print(out)
        return None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="新增文件：去重 → 分类 → 清单")
    ap.add_argument("--root", default=str(config.CORPUS_ROOT / config.INBOX_DIR_NAME))
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--check", action="store_true", help="只预览，不写库")
    ap.add_argument("--export", default=None, help="处理完导出可入库清单 CSV")
    ap.add_argument("--batch", default=None)
    args = ap.parse_args(argv)

    root = Path(args.root)
    if not root.exists():
        print(f"[info] 目录不存在：{root}（先建好再把文件拷进去）")
        return 0

    base = [str(TOOLS / "intake.py"), "--db", args.db, "--root", str(root)]
    if args.batch:
        base += ["--batch", args.batch]
    if args.check:
        base.append("--dry-run")
    print("=== 1/3 去重登记 ===")
    intake = run(base)
    if intake:
        print(json.dumps(intake["actions"], ensure_ascii=False))

    if args.check:
        print("\n[check] 预览模式，未写库；确认真实重后再去掉 --check")
        return 0

    print("\n=== 2/3 分类 ===")
    print(json.dumps(run([str(TOOLS / "classify.py"), "--db", args.db, "--status", status.PENDING]),
                     ensure_ascii=False))

    print("\n=== 3/3 可入库清单 ===")
    q = [str(TOOLS / "ingest_queue.py"), "--db", args.db, "--stats"]
    if args.export:
        q += ["--export", args.export]
    print(json.dumps(run(q), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
