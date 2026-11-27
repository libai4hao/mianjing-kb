"""生成答案侧评测基线：记录当前切题器对标注样本的输出，供修复前后对照。

用法（用带 openpyxl 的解释器运行）：
    python scripts/gen_answer_baseline.py
    python scripts/gen_answer_baseline.py --compare   # 重跑当前代码，对比基线差异

基线文件 eval/answer_baseline_v1.jsonl 每行一条 JSON：首行 meta（记录代码版本
sha256 与生成时间），其后每条样本记录 q/a 原文与噪声指标（分割线数、连续空行数）。
切题器代码变动后，--compare 会重跑并报告哪些样本的答案发生清洗/变化。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from openpyxl import load_workbook

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from mianjing_kb import chunker  # noqa: E402

CORPUS = Path(os.environ.get("KB_CORPUS_ROOT", r"<CORPUS_ROOT>")) / "20-题库MD"
XLSX = Path(os.environ.get("KB_ANNOTATION_XLSX", r"<ANNOTATION_XLSX>"))
BASELINE = REPO / "eval" / "answer_baseline_v1.jsonl"

HEADERS = [
    "sample_id", "qid", "doc_hash", "md_rel", "source_file", "span_start", "span_end",
    "answer_missing", "q_text", "a_text", "source_scope", "source_kind",
    "answer_authority", "canonical_eligible", "privacy_level", "scope_confidence",
    "unit_type", "is_question", "q_line_start", "q_line_end", "a_line_start", "a_line_end",
    "q_verdict", "a_verdict", "quality", "q_error_code", "a_error_code", "decision",
    "case_group", "rule_id", "corrected_q_text", "corrected_a_text", "note", "annotator",
    "annotated_at",
]

RE_SEP = re.compile(r"^\s*(?:-----+|\*\*\*+|___+)\s*$", re.M)
RE_BLANK_RUN = re.compile(r"\n[ \t]*\n[ \t]*\n")


def norm(s: str) -> str:
    return "".join(str(s or "").split())


def metrics(a: str) -> tuple[int, int]:
    return len(RE_SEP.findall(a)), len(RE_BLANK_RUN.findall(a))


def load_annotations() -> list[dict]:
    """#77：外部工作簿先过归档/展开预算，再限行限列读取——read_only 不限返回量。"""
    import zipfile

    max_archive = 64 * 1024 * 1024          # 压缩包本体
    max_uncompressed = 256 * 1024 * 1024    # 解压总量
    max_rows = 20_000                       # 应用级行数上限
    if XLSX.stat().st_size > max_archive:
        raise ValueError(f"标注工作簿过大: {XLSX.stat().st_size} bytes")
    with zipfile.ZipFile(XLSX) as zf:
        infos = zf.infolist()
        if len(infos) > 5000:
            raise ValueError(f"标注工作簿 ZIP 条目过多: {len(infos)}")
        total = sum(i.file_size for i in infos)
        if total > max_uncompressed:
            raise ValueError(f"标注工作簿解压总量超限: {total} bytes")
    wb = load_workbook(XLSX, data_only=True, read_only=True)
    try:
        ws = wb["母版"]
        if (ws.max_row or 0) > max_rows or (ws.max_column or 0) > len(HEADERS) + 8:
            raise ValueError(f"标注工作簿行列超限: {ws.max_row}x{ws.max_column}")
        out = []
        for r in ws.iter_rows(min_row=4, max_row=min(ws.max_row or 0, max_rows),
                              max_col=len(HEADERS), values_only=True):
            if r[0]:
                out.append(dict(zip(HEADERS, r)))
                if len(out) >= max_rows:
                    raise ValueError(f"标注行数超过上限 {max_rows}")
        return out
    finally:
        wb.close()


def snapshot() -> list[dict]:
    from mianjing_kb.kb import paths
    rows = load_annotations()
    cache: dict[str, list] = {}
    out = []
    for d in rows:
        rel = str(d["md_rel"])
        if rel not in cache:
            # #78：工作簿 md_rel 是外部输入——绝对路径 / .. / 链接一律拒绝，
            # 不得让标注表选到 Markdown 语料之外的文件。
            try:
                f = paths.safe_join(CORPUS, rel)
            except paths.UnsafePath:
                f = None
            cache[rel] = (
                chunker.chunk_v2(f.read_text(encoding="utf-8", errors="replace"))["items"]
                if f is not None and f.is_file() else []
            )
        items = cache[rel]
        q_labeled = str(d["q_text"] or "")
        anchor = norm(q_labeled)[:20]
        hit = next((it for it in items if anchor and anchor in norm(it["q"])), None)
        if hit is None:
            hit = next((it for it in items if norm(it["q"])[:20] in norm(q_labeled)), None)
        rec = {
            "_type": "sample",
            "sample_id": str(d["sample_id"]),
            "md_rel": rel,
            "q_labeled": q_labeled,
            "a_error_code": "" if d["a_error_code"] in (None, "") else str(d["a_error_code"]),
            "corrected_a": str(d["corrected_a_text"] or ""),
            "locate": "found" if hit else "notfound",
            "q_current": str(hit["q"]) if hit else "",
            "a_current": str(hit["a"]) if hit else "",
        }
        rec["sep"], rec["blank"] = metrics(rec["a_current"])
        rec["a_len"] = len(rec["a_current"])
        out.append(rec)
    return out


def code_sha() -> str:
    return hashlib.sha256((REPO / "mianjing_kb" / "chunker.py").read_bytes()).hexdigest()[:16]


def generate() -> None:
    recs = snapshot()
    meta = {
        "_type": "meta",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chunker_sha256_16": code_sha(),
        "n_samples": len(recs),
    }
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    with BASELINE.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for rec in recs:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    found = sum(1 for r in recs if r["locate"] == "found")
    print(f"已生成 {BASELINE} | 样本 {len(recs)} | 定位到当前题 {found} | 代码 sha {meta['chunker_sha256_16']}")


def compare() -> None:
    if not BASELINE.exists():
        sys.exit("基线不存在，请先不带 --compare 运行一次")
    lines = [json.loads(x) for x in BASELINE.read_text(encoding="utf-8").splitlines() if x.strip()]
    old_meta, old = lines[0], lines[1:]
    print(f"基线代码 sha {old_meta['chunker_sha256_16']} / 当前 {code_sha()}")
    cur = {r["sample_id"]: r for r in snapshot()}
    changed = 0
    for o in old:
        c = cur.get(o["sample_id"])
        if not c:
            continue
        if (c["a_current"] != o["a_current"] or c["sep"] != o["sep"] or c["blank"] != o["blank"]):
            changed += 1
            print(f"  {o['sample_id']}: sep {o['sep']}→{c['sep']} blank {o['blank']}→{c['blank']} "
                  f"len {o['a_len']}→{c['a_len']}")
    print(f"答案发生变化 {changed} / {len(old)} 条")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true", help="重跑当前代码并与基线对比")
    args = ap.parse_args()
    compare() if args.compare else generate()
