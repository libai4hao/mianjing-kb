# -*- coding: utf-8 -*-
"""`kb` 命令入口：把 31 个脚本收成一个入口，省掉 `python tools\\x.py` 的路径细节。

设计：本模块只做分发，不改变各脚本的行为——每个子命令都转调对应模块的 main(argv)，
子模块仍按既有方式自己 bootstrap sys.path，因此直接 `python tools\\intake.py` 依旧可用。

用法：
    kb list
    kb demo
    kb intake --help
    kb search "HashMap 扩容" --limit 5
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# 子命令 → 模块名（都在 tools/ 下）
COMMANDS = {
    "intake": "intake",
    "classify": "classify",
    "gate": "gate",
    "ingest-queue": "ingest_queue",
    "pipeline": "pipeline",
    "doc-convert": "doc_convert",
    "reconvert": "reconvert",
    "backfill": "backfill_convert",
    "ocr": "ocr_pages",
    "ocr-merge": "ocr_merge",
    "lazy-images": "lazy_images",
    "chunk": "chunker",
    "extract": "extract",
    "freq": "question_freq",
    "report": "report",
    "canonical": "canonical",
    "cluster": "cluster",
    "bookmarks": "bookmarks",
    "reconcile": "reconcile",
    "dispatch": "dispatch",
    "review": "review_cli",
    "build-db": "build_db",
    "embed": "embed_questions",
    "search": "search",
    "llm-smoke": "llm_smoke",
    "label": "batch_label",
    "probe": "probe_provider",
    "refresh": "refresh",
    "verify": "verify_ledger",
    "purge": "purge_origin",
    "demo": "demo",
    "eval": "eval_retrieval",
    "privacy": "privacy_scan",
}


def _bootstrap() -> None:
    for p in (str(REPO), str(REPO / "tools")):
        if p not in sys.path:
            sys.path.insert(0, p)


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print("kb <command> [args...]\n\n命令：\n  " + "\n  ".join(sorted(COMMANDS)))
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == "list":
        print("\n".join(sorted(COMMANDS)))
        return 0
    mod_name = COMMANDS.get(cmd)
    if not mod_name:
        print(f"[kb] 未知命令：{cmd}（kb list 看全部）", file=sys.stderr)
        return 2
    _bootstrap()
    mod = importlib.import_module(mod_name)
    entry = getattr(mod, "main", None)
    if entry is None:      # chunker 之类没有 main 的模块
        print(f"[kb] {cmd} 没有 main()，请直接看 {mod_name}.py 的 docstring", file=sys.stderr)
        return 2
    rc = entry(rest)
    return 0 if rc is None else int(rc)


if __name__ == "__main__":
    raise SystemExit(main())
