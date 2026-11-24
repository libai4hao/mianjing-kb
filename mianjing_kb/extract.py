# -*- coding: utf-8 -*-
"""extract：把语料切成题块落 questions 表（P1-1，chunker v2 产出）。

**单身份（P0-1，2026-11-19）**：题池只认一个文本源——有 md 产物的源文档用 md 正文、
身份仍是源 `doc_hash`；无产物的源才直接抽源文件；孤儿 md 保留自身 `md-mirror:` 身份。
映射由 `kb/mdmap.py` 统一裁决（hash8 精确 > 唯一 stem > 歧义裁决 > 孤儿），
报告落 `build/md_identity_report.json`。不再把"源文件 + md 镜像"同时收进题池。

- md/txt 直接读文件；pdf/docx/doc 走 question_freq.extract；<config.TEXT_MIN_CHARS 字符的空文档记 0 题
- 每题算 q_norm / q_norm_hash / simhash，落 meta/schema/004_questions.sql 的 questions 表
- 频次口径：精确同文直接可用视图 question_freq（见证数 = distinct collection，014 迁移）；
  近重复簇留给 P1-4 report（simhash）
- 状态与产物（Q-3）：开始处理写 `processing`，成功后写 `processed` 并记 `document_products`
  （含 size/mtime，供 `--incremental` 判"没变就跳过"）；失败留 `processing` 作为断点

用法：
    python -m mianjing_kb.extract --limit 20          # 试跑 20 件
    python -m mianjing_kb.extract --stats             # 只看 questions 表分布
    python -m mianjing_kb.extract --plan              # 只看"会抽哪些身份"（不读正文、不写库）
    python -m mianjing_kb.extract --incremental       # 只处理新增/变更文档
    python -m mianjing_kb.extract                     # 全量（幂等：同 doc 覆盖重写）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.kb import config, db, hashing, mdmap, products, status, textio, wlock  # noqa: E402
from mianjing_kb.kb.qtext import clean_question_text  # noqa: E402
from mianjing_kb.question_freq import close_word, extract, normalize, simhash  # noqa: E402
from mianjing_kb.chunker import chunk_v2  # noqa: E402

NOW_FMT = "%Y-%m-%dT%H:%M:%S"
MIN_CHARS = config.TEXT_MIN_CHARS
MIN_NORM = 4


_HEAD_LINE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+(.+?)(\r?\n)?$")
_STRUCT_HEAD = re.compile(
    r"^(?:\d{1,3}[.、)]|问题[一二三四五六七八九十0-9]+|第[一二三四五六七八九十0-9]+题|"
    r"[一二三四五六七八九十]+、)")


def _strip_nonstructural_headings(text: str) -> str:
    """只把"非题目型" markdown 标题去掉 `#`，保留编号题标题。

    chunker 能处理 `## 2. 什么是 UDP`，但整本 PDF 转出的 `# 供服务自动注册…` 这类
    断句标题如果全保留，会被当成标题题，反而把上下文切碎。编号/问题N/一、 这类
    结构性标题原样保留，其余标题只去掉标记、保留正文与空行。
    """
    out, in_fence = [], False
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip()
        if stripped.startswith(("```", "~~~")):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = _HEAD_LINE.match(line)
            if m and not _STRUCT_HEAD.match(m.group(1).strip()):
                out.append(m.group(1) + (m.group(2) or ""))
                continue
        out.append(line)
    return "".join(out)


def text_of(path: Path) -> str | None:
    """md/txt 读文件，其余走 question_freq.extract。

    返回 None 表示读取/解析失败；返回 "" 表示成功读到但不足 MIN_CHARS（或空文档）。
    结构性标题（`## 2.`、`问题N`、`一、`）原样保留给 chunker；非题目型的 PDF 断句标题只去
    `#` 标记、保留正文与空行，避免整本 PDF 的章节标题被当成标题题把上下文切碎。
    """
    if path.suffix.lower() in (".md", ".txt"):
        try:
            text = textio.read_text(path)
        except OSError:
            return None
    else:
        try:
            text = extract(path)
        except Exception:  # noqa: BLE001 —— 解析失败由调用方记 error，不冒充空文档
            return None
    text = unicodedata.normalize("NFKC", text or "")
    if len(text.strip()) < MIN_CHARS:
        return ""
    return _strip_nonstructural_headings(text)


def _excluded_rels() -> tuple[set[str], set[str]]:
    """P2-⑤ 排除表：返回 (源文件 rel 集, md 镜像 rel 集)，正反斜杠归一。"""
    if not config.EXCLUSIONS_FILE.exists():
        return set(), set()
    data = json.loads(config.EXCLUSIONS_FILE.read_text(encoding="utf-8"))
    src = {x.get("rel", "").replace("\\", "/") for x in data.get("items", []) if x.get("rel")}
    mds = {x.get("md_rel", "").replace("\\", "/") for x in data.get("items", []) if x.get("md_rel")}
    return src, mds


def _source_rels(conn) -> dict[str, tuple[str, str, str, str]]:
    """doc_hash → (源相对路径, status, 文件名 stem, doc_hash 前 8 位)。

    优先用 DB 的 dir_path/file_name，不靠 relative_to 路径运算（Windows 盘符不一致会退化）。
    """
    out = {}
    for r in conn.execute("SELECT doc_hash, dir_path, file_name, src_path, status "
                          "FROM source_documents"):
        dir_path = (r["dir_path"] or "").replace("\\", "/").strip("/")
        name = r["file_name"] or Path(r["src_path"]).name
        rel = f"{dir_path}/{name}" if dir_path else name
        out[r["doc_hash"]] = (rel, r["status"], Path(name).stem.lower(), r["doc_hash"][:8])
    return out


def _orphan_owned_by_retired_source(rel: str, source_rels: dict) -> bool:
    """孤儿 md 若其实是某条非内容池源的产物，跳过——否则隔离/废弃件会以 md-mirror 复活。"""
    stem = Path(rel).stem
    hit = mdmap.HASH_SUFFIX.search(stem)
    base = mdmap.base_stem(stem).lower()
    for _src_rel, st, fn_stem, prefix in source_rels.values():
        if st in status.INGESTED:
            continue
        if hit and hit.group(1) == prefix:
            return True
        if base == mdmap.base_stem(fn_stem).lower():
            return True
    return False


def doc_pool(conn, md_root: Path | None = None) -> tuple[list[tuple[str, Path]], "mdmap.Mapping"]:
    """(doc_hash, path) 题池 —— **一文档一身份**。

    1. 有 md 产物：用 md 正文，身份 = 源 doc_hash（P0-1 单身份）；
    2. 无产物：抽源文件，身份 = 源 doc_hash；
    3. 孤儿 md（找不到源）：保留 `md-mirror:` 身份单独成池。
    exclusions.json 同时校验源 rel 与 md rel；产物命中时整篇不抽，不回退源文件。
    """
    ex_src, ex_md = _excluded_rels()
    source_rels = _source_rels(conn)
    ing = set(status.INGESTED)
    mapping = mdmap.build(conn, Path(md_root or config.QUESTION_MD_DIR))
    pool: dict[str, Path] = {}

    for doc_hash, (md_rel, p) in mapping.by_doc.items():
        src = source_rels.get(doc_hash)
        # mdmap.build 已按 INGESTED 过滤；这里再兜一层，隔离/废弃/驳回件绝不进池
        if not src or src[1] not in ing:
            continue
        if src[0] in ex_src or md_rel in ex_md:
            continue
        pool[doc_hash] = p

    for r in conn.execute(
            f"SELECT doc_hash, src_path FROM source_documents WHERE status IN {status.sql_in(status.INGESTED)}").fetchall():
        doc_hash = r["doc_hash"]
        if doc_hash in pool:             # 已有产物，源文件不再重复抽
            continue
        p = Path(r["src_path"])
        if not p.exists():
            continue
        rel = source_rels.get(doc_hash, ("", ""))[0]
        if rel in ex_src:
            continue
        pool[doc_hash] = p

    for mh, (rel, p) in mapping.orphans.items():
        if rel in ex_md:
            continue
        if _orphan_owned_by_retired_source(rel, source_rels):
            continue
        pool.setdefault(mh, p)
    # 按产物路径排序，--limit 试跑可复现（不按 doc_hash 随机切）
    return sorted(pool.items(), key=lambda kv: str(kv[1])), mapping


def write_identity_report(mapping) -> Path:
    """`build/md_identity_report.json`：映射率、歧义、多产物、孤儿——单身份的可审计留痕。"""
    build = Path(config.BUILD_DIR)
    build.mkdir(parents=True, exist_ok=True)
    out = build / "md_identity_report.json"
    out.write_text(json.dumps(mapping.report, ensure_ascii=False, indent=1), encoding="utf-8")
    return out


def _product_loc(conn, doc_hash: str, mapping) -> tuple[str | None, str | None, str]:
    """(md_rel, md_hash, text_source)：返回本次成功抽取所用 Markdown 的当前 hash。

    映射同步阶段刻意保留上次成功抽取的 hash 供增量比较，因此不能从台账复用旧值；
    只有本次抽取成功后，才将当前文件 hash 提交为新的成功指纹。
    """
    prod = products.get(conn, doc_hash)
    if doc_hash in mapping.by_doc:
        rel, path = mapping.by_doc[doc_hash]
        return rel, hashing.doc_hash(path), "md"
    if doc_hash in mapping.orphans:
        rel, path = mapping.orphans[doc_hash]
        return rel, hashing.doc_hash(path), "orphan"
    return None, prod["md_hash"] if prod else None, "source"


def mark_status(conn, doc_hashes: list[str], new_status: str, only_from=status.INGESTED) -> int:
    """批量改 source_documents.status（只动内容池里的行；孤儿 md 没有台账行，自然跳过）。"""
    n = 0
    for i in range(0, len(doc_hashes), 500):
        part = doc_hashes[i:i + 500]
        cur = conn.execute(
            f"UPDATE source_documents SET status=? WHERE doc_hash IN ({','.join('?' * len(part))}) "
            f"AND status IN {status.sql_in(only_from)}", [new_status, *part])
        n += cur.rowcount
    return n


def purge_orphans(conn, pool_keys: set[str], *, allow_state_loss: bool = False) -> int:
    """清理孤儿题行；已治理数据未被显式授权丢弃时整批拒绝。"""
    stale = [r[0] for r in conn.execute("SELECT DISTINCT doc_hash FROM questions").fetchall()
             if r[0] not in pool_keys]
    stale_docs = set(stale)
    for doc_hash in stale:
        _prepare_rows_preserving_state(
            conn, doc_hash, [], allow_state_loss=allow_state_loss, removing_docs=stale_docs)
    removed = 0
    for i in range(0, len(stale), 500):
        chunk = stale[i:i + 500]
        conn.execute(f"DELETE FROM questions WHERE doc_hash IN ({','.join('?' * len(chunk))})", chunk)
        removed += len(chunk)
    return removed


def _signed64(v: int) -> int:
    """simhash 为无符号 64 位，SQLite INTEGER 是有符号——转二补码。"""
    return v - (1 << 64) if v >= (1 << 63) else v


def _rows_from_text(doc_hash: str, text: str) -> list[dict]:
    rows = []
    v2 = chunk_v2(text)
    for it in v2["items"]:
        raw_q = it["q"]
        qn = normalize(raw_q)          # F4：身份仍按原串算，避免 q_key 漂移
        if len(qn) < MIN_NORM:
            continue
        q_norm_hash = hashlib.sha256(qn.encode("utf-8")).hexdigest()[:16]
        rows.append({
            "doc_hash": doc_hash,
            "seq": it["seq"],
            "q_text": clean_question_text(raw_q)[:2000],   # 只洗显示文本，a_text 不动
            "a_text": it["a"][:20000],
            "span_start": it["span"][0],
            "span_end": it["span"][1],
            "qtype": it["qtype"],
            "conf": it["conf"],
            "q_norm": qn[:500],
            "q_norm_hash": q_norm_hash,
            "q_key": f"{doc_hash}:{q_norm_hash}",
            "simhash": _signed64(simhash(qn)),
            "extracted_at": datetime.now().strftime(NOW_FMT),
        })
    # 同 q_key 只留答案最长的一条（并列取先出现的 seq）；顺序按 seq 保持题号稳定。
    keep: dict[str, dict] = {}
    for r in rows:
        old = keep.get(r["q_key"])
        if old is None or len(r["a_text"]) > len(old["a_text"]):
            keep[r["q_key"]] = r
    return sorted(keep.values(), key=lambda r: r["seq"])


def extract_doc_status(doc_hash: str, path: Path) -> tuple[list[dict], str]:
    """返回 (rows, status)。status = ok / empty / no_questions / read_error。"""
    text = text_of(path)
    if text is None:
        return [], "read_error"
    if not text:
        return [], "empty"
    rows = _rows_from_text(doc_hash, text)
    return rows, "ok" if rows else "no_questions"


def extract_doc(doc_hash: str, path: Path) -> list[dict]:
    """单文档 → questions 行列表（不落库）。

    T29：q_key=(doc_hash,q_norm_hash) 是跨库稳定键；同一文档内同题重复（chunker
    重复产出）先按"答案最长"保留一条，保证 q_key 唯一。
    """
    rows, _status = extract_doc_status(doc_hash, path)
    return rows


class StateLossError(RuntimeError):
    """重抽或孤儿清理会丢弃无法对齐的已治理数据。"""

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


def _prepare_rows_preserving_state(conn, doc_hash: str, rows: list[dict], *,
                                   allow_state_loss: bool = False,
                                   loss_report: dict | None = None,
                                   removing_docs: set[str] | None = None) -> list[dict]:
    columns = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
    answer_cols = tuple(c for c in (
        "a_text", "answer_state", "answer_source", "answer_model", "answered_at"
    ) if c in columns)
    label_cols = tuple(c for c in (
        "topic", "qtype", "difficulty", "tags", "position", "quality", "canonical_q"
    ) if c in columns)
    tracked = answer_cols + label_cols
    select_cols = ["qid", "q_norm_hash", *tracked]
    old_rows = [dict(zip(select_cols, row)) for row in conn.execute(
        f"SELECT {','.join(select_cols)} FROM questions WHERE doc_hash=? ORDER BY qid", (doc_hash,)
    ).fetchall()]
    new_rows = [dict(r) for r in rows]

    def present(value) -> bool:
        if value is None:
            return False
        return str(value).strip() not in ("", "[]")

    def has_provenance(old: dict) -> bool:
        provenance_cols = ("answer_source", "answer_model", "answered_at")
        return any(present(old.get(c)) for c in provenance_cols)

    def governed(old: dict) -> bool:
        if has_provenance(old):
            return True
        if any(present(old.get(c)) for c in label_cols if c != "qtype"):
            return True
        return old.get("qtype") not in (None, "", "八股")

    hash_counts = Counter(str(r.get("q_norm_hash") or "") for r in new_rows)
    new_hashes = set(hash_counts) - {""}
    duplicates = sorted(h for h, count in hash_counts.items() if h and count > 1)
    if duplicates:
        raise StateLossError(
            f"重抽 {doc_hash} 的新结果包含重复 q_norm_hash={duplicates[0]}；当前文档未被替换。",
            details={"doc_hash": doc_hash, "reason": "duplicate_new_hash",
                     "q_norm_hashes": duplicates})

    old_by_hash: dict[str, list[dict]] = {}
    unmatched_qids = []
    provenance_qids = []
    for old in old_rows:
        h = str(old.get("q_norm_hash") or "")
        if h and h in new_hashes:
            old_by_hash.setdefault(h, []).append(old)
        elif governed(old):
            unmatched_qids.append(old.get("qid"))
            if has_provenance(old):
                provenance_qids.append(old.get("qid"))

    excluded_docs = set(removing_docs or ())
    excluded_docs.add(doc_hash)
    lost_fact_hashes = []
    if "label_facts" in {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}:
        for row in conn.execute(
            "SELECT DISTINCT lf.q_norm_hash FROM label_facts lf "
            "JOIN questions q ON q.q_norm_hash=lf.q_norm_hash WHERE q.doc_hash=?", (doc_hash,)
        ):
            h = str(row[0] or "")
            if h and h not in new_hashes:
                refs = conn.execute(
                    "SELECT DISTINCT doc_hash FROM questions WHERE q_norm_hash=?", (h,))
                if not any(str(r[0] or "") not in excluded_docs for r in refs):
                    lost_fact_hashes.append(h)

    loss = {"doc_hash": doc_hash, "reason": "unmatched_governed_questions",
            "qids": unmatched_qids, "count": len(unmatched_qids),
            "provenance_qids": provenance_qids, "provenance_count": len(provenance_qids),
            "lost_fact_hashes": lost_fact_hashes, "lost_fact_count": len(lost_fact_hashes)}
    if loss_report is not None:
        loss_report.update(loss)
    if (unmatched_qids or lost_fact_hashes) and not allow_state_loss:
        sample_qids = ",".join(str(x) for x in unmatched_qids[:10]) or "-"
        sample_hashes = ",".join(lost_fact_hashes[:5]) or "-"
        raise StateLossError(
            f"重抽 {doc_hash} 将丢失已治理数据：未匹配题目 {len(unmatched_qids)} 条 "
            f"(qid={sample_qids})，失去最后引用的 label_facts {len(lost_fact_hashes)} 条 "
            f"(hash={sample_hashes})。先核对 chunker/题干变化；确认接受丢失后显式加 "
            "--allow-state-loss。当前文档未被替换。", details=loss)

    for h, matches in old_by_hash.items():
        signatures = {tuple(str(r.get(c) or "") for c in tracked) for r in matches}
        if len(signatures) > 1:
            qids = [r.get("qid") for r in matches]
            raise StateLossError(
                f"重抽 {doc_hash} 的 q_norm_hash={h} 对应多条旧记录且治理字段冲突；"
                "请先人工处理重复题，当前文档未被替换。",
                details={"doc_hash": doc_hash, "reason": "conflicting_duplicate_hash",
                         "q_norm_hash": h, "qids": qids})

    old_by_hash_one = {h: matches[0] for h, matches in old_by_hash.items()}
    for row in new_rows:
        for col in tracked:
            row.setdefault(col, "" if col == "answer_source" else None)
        old = old_by_hash_one.get(str(row.get("q_norm_hash") or ""))
        if old is None:
            continue
        if not present(row.get("a_text")):
            row["a_text"] = old.get("a_text")
            for col in answer_cols:
                if col != "a_text":
                    row[col] = old.get(col)
        elif "answer_state" in columns:
            row["answer_state"] = "ok"
        for col in label_cols:
            if present(old.get(col)):
                row[col] = old.get(col)

    if new_rows:
        keys = set(new_rows[0])
        if any(set(r) != keys for r in new_rows[1:]):
            raise ValueError("rows 的列集合必须一致")
    return new_rows


def store(conn, doc_hash: str, rows: list[dict], *, allow_state_loss: bool = False,
          loss_report: dict | None = None) -> int:
    """按稳定题干 hash 继承答案和治理字段，再替换该文档的题目。"""
    rows = _prepare_rows_preserving_state(
        conn, doc_hash, rows, allow_state_loss=allow_state_loss, loss_report=loss_report)
    conn.execute("DELETE FROM questions WHERE doc_hash=?", (doc_hash,))
    if rows:
        cols = ",".join(rows[0])
        ph = ",".join("?" for _ in rows[0])
        conn.executemany(
            f"INSERT INTO questions ({cols}) VALUES ({ph})",
            [[r[c] for c in rows[0]] for r in rows],
        )
    cur = conn.execute(
        "UPDATE questions SET "
        " topic=(SELECT lf.topic FROM label_facts lf WHERE lf.q_norm_hash=questions.q_norm_hash), "
        " qtype=COALESCE((SELECT lf.qtype FROM label_facts lf "
        "                WHERE lf.q_norm_hash=questions.q_norm_hash), qtype), "
        " difficulty=(SELECT lf.difficulty FROM label_facts lf "
        "             WHERE lf.q_norm_hash=questions.q_norm_hash), "
        " tags=(SELECT lf.tags FROM label_facts lf WHERE lf.q_norm_hash=questions.q_norm_hash) "
        "WHERE doc_hash=? AND q_norm_hash IN (SELECT q_norm_hash FROM label_facts)", (doc_hash,))
    return cur.rowcount


def _gate_statuses(conn) -> tuple[dict, dict]:
    """source_documents.gate_result + gate_results 最新 verdict，供 T30 前置过滤。"""
    src = {r["doc_hash"]: r["gate_result"] for r in conn.execute(
        "SELECT doc_hash, gate_result FROM source_documents "
        "WHERE gate_result IS NOT NULL AND gate_result <> ''")}
    latest = {}
    for r in conn.execute("SELECT doc_hash, verdict, checked_at FROM gate_results "
                          "WHERE doc_hash IS NOT NULL ORDER BY checked_at"):
        latest[r["doc_hash"]] = r["verdict"]
    return src, latest


def _filter_by_gate(pool, statuses, require_gate: bool):
    """T30：只放行 pass；review/fail 记原因跳过。--require-gate 时无台账也跳过。"""
    src, latest = statuses
    kept, skipped = [], {}
    for doc_hash, p in pool:
        gate = src.get(doc_hash) or latest.get(doc_hash)
        if gate and gate != "pass":
            skipped[gate] = skipped.get(gate, 0) + 1
            continue
        if not gate and require_gate:
            skipped["ungated"] = skipped.get("ungated", 0) + 1
            continue
        kept.append((doc_hash, p))
    return kept, skipped


def _read_doc_list(path: str) -> set[str]:
    """定向补切清单：每行一个 doc_hash，空行与 # 注释忽略。"""
    out: set[str] = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.add(s)
    return out


def _docs_with_answer_provenance(conn, doc_hashes: list[str]) -> set[str]:
    """题里带模型答案或补答溯源的文档，供只读计划展示风险面。"""
    columns = {r[1] for r in conn.execute("PRAGMA table_info(questions)")}
    tracked = [c for c in ("answer_source", "answer_model", "answered_at") if c in columns]
    if not tracked:
        return set()
    guard = " OR ".join(f"COALESCE({c},'')<>''" for c in tracked)
    out: set[str] = set()
    for i in range(0, len(doc_hashes), 500):
        part = doc_hashes[i:i + 500]
        out.update(r[0] for r in conn.execute(
            f"SELECT DISTINCT doc_hash FROM questions WHERE doc_hash IN ({','.join('?' * len(part))}) "
            f"AND ({guard})", part))
    return out


def _run(args) -> int:
    read_only = bool(args.dry_run or args.plan or args.stats)
    conn = db.connect(args.db, create=not read_only, readonly=read_only)
    if args.stats:
        n = conn.execute("SELECT COUNT(*) FROM questions").fetchone()[0]
        out = {"questions": n,
               "by_doc": conn.execute("SELECT COUNT(DISTINCT doc_hash) FROM questions").fetchone()[0],
               "qtype": dict(conn.execute("SELECT qtype, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC").fetchall()),
               "conf": dict(conn.execute("SELECT conf, COUNT(*) FROM questions GROUP BY 1 ORDER BY 2 DESC").fetchall()),
               "freq_top10": [dict(r) for r in conn.execute(
                   "SELECT sample_q, n_distinct_docs, n_distinct_collections FROM question_freq "
                   "ORDER BY n_distinct_collections DESC LIMIT 10").fetchall()]}
        print(json.dumps(out, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    pool, mapping = doc_pool(conn)
    pool_all = pool
    pool, gate_skipped = _filter_by_gate(pool, _gate_statuses(conn), args.require_gate)
    doc_list_missing = 0
    if getattr(args, "doc_list", None):
        wanted = _read_doc_list(args.doc_list)
        doc_list_missing = len(wanted - {h for h, _p in pool_all})
        pool = [x for x in pool if x[0] in wanted]
    # 只读计划展示补答溯源风险面；实际重抽按 hash 继承，不再整篇跳过。
    provenance_docs = _docs_with_answer_provenance(conn, [h for h, _p in pool])
    report_path = None if (args.plan or args.dry_run) else write_identity_report(mapping)
    if args.plan:
        print(json.dumps({
            "pool_docs": len(pool_all),
            "pool_after_gate": len(pool),
            "skipped_gate": gate_skipped,
            "skipped_doc_list_missing": doc_list_missing,
            "answer_provenance_docs": len(provenance_docs),
            "md_mapped_sources": len(mapping.by_doc),
            "source_only": len(pool_all) - len(mapping.by_doc) - len(mapping.orphans),
            "orphan_md": len(mapping.orphans),
            "mapping": {k: v for k, v in mapping.report.items()
                        if k not in ("mapping", "conflicts", "duplicates", "orphans_list")},
            "report": None,
        }, ensure_ascii=False, indent=1))
        conn.close()
        return 0

    # 产物台账先落"映射事实"（extracted_at 留空），增量判据才有输入
    if not args.dry_run:
        products.sync_mapping(conn, mapping)
        conn.commit()
    pool_total = len(pool)
    if args.incremental:
        pool = [x for x in pool if products.needs_work(conn, x[0], x[1])]
    skipped_unchanged = pool_total - len(pool)
    if args.limit:
        pool = pool[:args.limit]
    started = time.perf_counter()
    if not args.dry_run and pool:
        mark_status(conn, [h for h, _p in pool], status.PROCESSING)
        conn.commit()
    n_docs = n_q = n_empty = n_err = n_reattached = n_answer_lost = 0
    n_state_loss_blocked = 0
    state_losses: list[dict] = []
    n_empty_preserved = 0
    qtype: dict[str, int] = {}
    conf: dict[str, int] = {}
    for i, (doc_hash, p) in enumerate(pool):
        try:
            rows, read_state = extract_doc_status(doc_hash, p)
        except Exception as ex:  # noqa: BLE001
            n_err += 1
            # 失败留 processing：状态本身就是"下次从这续跑"的标记
            print(f"[warn] {p.name}: {type(ex).__name__}: {ex}", file=sys.stderr)
            continue
        if read_state == "read_error":
            n_err += 1
            print(f"[warn] {p.name}: 读取/解析失败，保留原记录", file=sys.stderr)
            continue
        n_docs += 1
        if not rows:
            n_empty += 1
        n_q += len(rows)
        for r in rows:
            qtype[r["qtype"]] = qtype.get(r["qtype"], 0) + 1
            conf[r["conf"]] = conf.get(r["conf"], 0) + 1
        if not args.dry_run:
            old_n = conn.execute("SELECT COUNT(*) FROM questions WHERE doc_hash=?",
                                 (doc_hash,)).fetchone()[0]
            if not rows and old_n and not args.allow_empty_overwrite:
                n_empty_preserved += 1
                print(f"[warn] {p.name}: 本次 0 题但已有 {old_n} 条旧题，保留旧题不删除"
                      f"（确认清空请加 --allow-empty-overwrite）", file=sys.stderr)
                continue
            loss_report: dict = {}
            conn.execute("SAVEPOINT extract_one")
            try:
                n_reattached += store(
                    conn, doc_hash, rows,
                    allow_state_loss=(args.allow_state_loss
                                      or (not rows and args.allow_empty_overwrite)),
                    loss_report=loss_report)
                n_answer_lost += loss_report.get("provenance_count", 0)
                md_rel, md_hash, text_source = _product_loc(conn, doc_hash, mapping)
                products.upsert(conn, doc_hash, md_rel=md_rel, md_hash=md_hash,
                                text_source=text_source, input_path=p, questions_n=len(rows),
                                extracted_at=datetime.now().strftime(NOW_FMT))
                mark_status(conn, [doc_hash], status.PROCESSED)
            except StateLossError as ex:
                conn.execute("ROLLBACK TO extract_one")
                conn.execute("RELEASE extract_one")
                n_err += 1
                n_state_loss_blocked += 1
                state_losses.append(ex.details)
                print(f"[warn] {p.name}: {ex}", file=sys.stderr)
                continue
            except Exception as ex:  # noqa: BLE001
                conn.execute("ROLLBACK TO extract_one")
                conn.execute("RELEASE extract_one")
                n_err += 1
                print(f"[warn] {p.name}: 落库失败 {type(ex).__name__}: {ex}", file=sys.stderr)
                continue
            else:
                conn.execute("RELEASE extract_one")
            if (i + 1) % 50 == 0:            # 增量/长批次的分块提交（崩溃只回退当前块）
                conn.commit()
        if (i + 1) % 200 == 0:
            print(f"[prog] {i + 1}/{len(pool)} docs, {n_q} questions", file=sys.stderr)
    close_word()        # Word COM 只在抽取阶段用；批次结束立刻释放，不带着宿主进程进落库/统计
    if not args.dry_run:
        # 全量才清孤儿；--limit/大量报错时禁用清理，避免把未处理部分误判为孤儿
        purge_skipped = None
        if args.incremental:
            purge_skipped = "incremental"
        elif getattr(args, "doc_list", None):
            purge_skipped = "doc-list"
        elif args.limit:
            purge_skipped = "limit"
        elif n_err > max(1, int(len(pool_all) * 0.05)):
            purge_skipped = "errors"
        n_purged = 0
        if not purge_skipped:
            try:
                n_purged = purge_orphans(conn, {k for k, _ in pool_all},
                                         allow_state_loss=args.allow_state_loss)
            except StateLossError as ex:
                n_err += 1
                n_state_loss_blocked += 1
                state_losses.append(ex.details)
                purge_skipped = "state-loss"
                print(f"[warn] 孤儿清理已拒绝：{ex}", file=sys.stderr)
        n_lost = conn.execute("SELECT COUNT(*) FROM label_facts WHERE q_norm_hash NOT IN "
                              "(SELECT q_norm_hash FROM questions)").fetchone()[0]
        if n_lost:
            print(f"[warn] label_facts 有 {n_lost} 条打标已无对应题（重切后真正丢失），"
                  f"详见 docs/项目审核报告/修复方案-2026-11-19.md F1", file=sys.stderr)
        conn.commit()
    else:
        purge_skipped, n_lost = None, 0
    elapsed = time.perf_counter() - started
    print(json.dumps({
        "docs": n_docs, "questions": n_q, "empty_docs": n_empty,
        "empty_preserved": n_empty_preserved, "errors": n_err,
        "incremental": args.incremental, "skipped_unchanged": skipped_unchanged,
        "skipped_gate": gate_skipped,
        "skipped_doc_list_missing": doc_list_missing,
        "answer_provenance_skipped": 0,
        "answer_provenance_lost": n_answer_lost if not args.dry_run else 0,
        "state_loss_blocked": n_state_loss_blocked,
        "state_loss": state_losses,
        "purged_orphan_docs": n_purged if not args.dry_run else 0,
        "purge_skipped": purge_skipped,
        "label_reattached": n_reattached if not args.dry_run else 0,
        "label_facts_lost": n_lost,
        "dry_run": args.dry_run, "elapsed_sec": round(elapsed, 1),
        "identity": {"md_mapped_sources": len(mapping.by_doc),
                     "orphan_md": len(mapping.orphans),
                     "ambiguous": mapping.report.get("ambiguous", 0),
                     "multi_md_docs": mapping.report.get("docs_multi_md", 0),
                     "report": str(report_path)},
        "products": products.summary(conn) if not args.dry_run else {},
        "qtype": qtype, "conf": conf,
    }, ensure_ascii=False, indent=1))
    conn.close()
    # 有抽取/落库失败必须让自动化看到非零：仅靠 processing 状态无法区分“全成功/部分成功”。
    return 1 if n_err else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="P1-1：题块落 questions 表")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stats", action="store_true", help="只看 questions 表分布")
    ap.add_argument("--plan", action="store_true",
                    help="只看身份映射与题池规模（不读正文、不写库）")
    ap.add_argument("--incremental", action="store_true",
                    help="只处理新增/变更文档（按 document_products 的 size/mtime 判定）")
    ap.add_argument("--require-gate", action="store_true",
                    help="T30：无 gate 结论的文档也跳过（默认只跳过已知 review/fail）")
    ap.add_argument("--allow-empty-overwrite", action="store_true",
                    help="允许 0 题结果覆盖已有旧题（默认保留旧题并告警）")
    ap.add_argument("--doc-list",
                    help="只处理清单里的 doc_hash（每行一个，# 开头为注释）；"
                         "用于定向补切，自动跳过孤儿清理与全量统计口径")
    ap.add_argument("--allow-state-loss", "--allow-answer-loss", dest="allow_state_loss",
                    action="store_true",
                    help="显式允许丢弃无法按 q_norm_hash 对齐的补答溯源、治理字段或最后一份标签事实")
    args = ap.parse_args(argv)
    if args.limit < 0:
        # Python 切片允许负数：--limit -1 会处理"除最后 1 件之外的整个池"（ISSUE-07）
        ap.error("--limit 必须大于等于 0（0 表示不限制）")
    if args.dry_run or args.plan or args.stats:      # 只读，不进写锁
        return _run(args)
    db_path = Path(args.db)
    try:
        with wlock.write_lock("kb", tool="extract", build_dir=db_path.parent, target=db_path):
            return _run(args)
    except wlock.LockHeld as ex:
        print(json.dumps({"verdict": "ABORT", "why": str(ex),
                          "note": "另有工具在写 kb.sqlite；extract 不与之并行，稍后重试"},
                         ensure_ascii=False, indent=1))
        return 2


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
