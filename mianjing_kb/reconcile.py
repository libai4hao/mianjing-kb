# -*- coding: utf-8 -*-
"""P1-2 reconcile：convert_log ok 件 vs 20-题库MD 实存 md 逐一对账。

输出路径约定：20-题库MD/<category>/<topic>/<stem>.md（转换时的 category/topic）。
差额归因顺序：
1. 输出在别的主题目录（按 stem 全树搜——转换后被重分类/移动过）
2. 源件退役 superseded（V1.1 乱码替换 / 99 区判重删除等，note 有留痕）
3. 源件在 99 隔离区或删除留痕
4. 源件物理不存在
其余 → 人工核对。

输出：build/reconcile_p12.json + 控制台摘要
"""
import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mianjing_kb.kb import db, config, status  # noqa: E402

# P1-3 修复（2026-11-17 审核）：原为硬编码绝对路径且定义在 kb import 之前，
# KB_* 环境变量覆盖对本脚本完全失效；统一走 config，换机器不再坏。
BUILD = Path(config.BUILD_DIR)
MD_ROOT = Path(config.QUESTION_MD_DIR)


def main() -> int:
    log = json.loads((BUILD / "convert_log.json").read_text(encoding="utf-8"))
    ok = [r for r in log if r.get("ok")]
    actual_paths = {p.relative_to(MD_ROOT).as_posix() for p in MD_ROOT.rglob("*.md")}
    by_stem: dict[str, list[str]] = {}
    for rel in actual_paths:
        by_stem.setdefault(Path(rel).stem, []).append(rel)
    conn = db.connect(config.DB_PATH, create=False)
    sd = {r["file_name"]: dict(r) for r in conn.execute(
        "SELECT file_name, status, dir_path, note FROM source_documents").fetchall()}

    del_log = set()
    try:
        with open(BUILD / "99_deletion_log.csv", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                for k in ("file_name", "文件名", "name", "rel", "path"):
                    v = (row.get(k) or "").strip()
                    if v:
                        del_log.add(Path(v).name)
    except Exception:
        pass

    missing, matched = [], 0
    for r in ok:
        stem = Path(r["rel"]).stem
        expected = f"{r['category']}/{r['topic']}/{stem}.md"
        if expected in actual_paths:
            matched += 1
            continue
        hits = by_stem.get(stem, [])
        if hits:
            reason = "输出在别的主题目录（转换后被重分类/移动）：" + "; ".join(hits[:2])
        else:
            src_name = Path(r["rel"]).name
            rec = sd.get(src_name)
            if rec and rec["status"] == status.SUPERSEDED:
                reason = f"源件退役 superseded：{(rec['note'] or '')[:90]}"
            elif rec and (rec["dir_path"] or "").startswith("99-重复文件待删"):
                reason = "源件在99隔离区待物理删"
            elif src_name in del_log or stem in del_log:
                reason = "源件在删除留痕（99_deletion_log/origin_deleted）"
            elif not (config.CORPUS_ROOT / r["rel"]).exists():
                reason = "源件物理不存在（已移动/删除）"
            else:
                reason = "需人工核对"
        missing.append({"rel": r["rel"], "expected_md": expected, "reason": reason})

    logged_stems = {Path(r["rel"]).stem for r in ok}
    extra = sorted(rel for rel in actual_paths if Path(rel).stem not in logged_stems)

    out = {"ok_in_log": len(ok), "md_files": len(actual_paths),
           "matched_by_path": matched, "missing": missing, "extra_unexpected": extra}
    (BUILD / "reconcile_p12.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok_in_log": len(ok), "md_files": len(actual_paths),
                      "matched": matched, "missing": len(missing),
                      "reason_dist": dict(Counter(m["reason"].split("：")[0] for m in missing)),
                      "extra": extra}, ensure_ascii=False, indent=1))
    conn.close()
    return 0


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
