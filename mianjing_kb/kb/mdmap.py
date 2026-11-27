# -*- coding: utf-8 -*-
"""md 产物 → 源文档身份映射（P0-1「题池单身份」的唯一实现）。

背景（`docs/27` P0-1）：`extract` 原来把源文件（内容 hash）和 `20-题库MD` 镜像
（`md-mirror:` 路径 hash）当两个身份收进题池，同一原文被抽两遍，32,087 题里 64% 挂镜像身份，
见证数被系统性放大。修复的核心不是"删点重复"。而是**先定身份**：

    md 是源文档的"产物"，产物不带自己的身份；题行的 doc_hash 一律用源文档的 doc_hash。

映射优先级（确定性，不猜内容）：

1. **hash8 精确**：md 文件名带 `_<8hex>` / `__<8hex>` 后缀，且该 8 位是某个源 `doc_hash` 的前缀 → 唯一命中即映射；
2. **唯一 stem**：剥掉 hash 后缀后与源文件名 stem（归一化后）相等，且只有一个源 → 映射；
3. **歧义**：多个候选时只有产物字节与某候选源 sha256 一致才归属该源（审计候选 #36）；
   否则记 `conflicts` 并留孤儿——各源回退抽自己的源文件，**绝不按文件名猜归属**；
4. **孤儿**：没有源文档对应（例如纯 md 整理件），保留自身 `md-mirror:` 身份，计入 `orphans`。

一个源有多份产物（reconvert 新旧版、跨目录重复导出）时只取**主产物**：
带 hash8 后缀 > 文本更大 > rel 字典序；其余记 `duplicates`，不进题池（否则同一身份下题行重复）。

本模块只读文件系统与台账，不写库；报告由调用方落盘（`build/md_identity_report.json`）。
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from mianjing_kb.kb import status

HASH_SUFFIX = re.compile(r"(?:__|_)([0-9a-f]{8})$")
MD_EXTS = (".md", ".txt")


def mirror_hash(md_rel: str) -> str:
    """孤儿 md 的身份：与 extract/build_db/verify 同一口径。"""
    return hashlib.sha256(("md-mirror:" + md_rel).encode("utf-8")).hexdigest()[:16]


def base_stem(stem: str) -> str:
    """剥 `<stem>_<hash8>` / `<stem>__<hash8>`（单/双下划线两种历史口径）。"""
    return HASH_SUFFIX.sub("", stem)


def _norm(stem: str) -> str:
    return stem.strip().lower()


class Mapping:
    """一次扫描的结果。`by_doc[doc_hash] = (md_rel, path)` 只含主产物。"""

    def __init__(self):
        self.by_doc: dict[str, tuple[str, Path]] = {}
        self.orphans: dict[str, tuple[str, Path]] = {}      # mirror_hash -> (rel, path)
        self.products: list[dict] = []                      # 全量 md：rel/hash/doc_hash/selected
        self.report: dict = {}

    def md_rel_of(self, doc_hash: str) -> str:
        return self.by_doc[doc_hash][0]

    def path_of(self, doc_hash: str) -> Path:
        return self.by_doc[doc_hash][1]


def build(conn, md_root: Path) -> Mapping:
    """扫 md 目录 + 内容池台账（classified/processing/processed），产出映射与报告（纯只读）。"""
    m = Mapping()
    rows = conn.execute(
        f"SELECT doc_hash, file_name FROM source_documents WHERE status IN {status.sql_in(status.INGESTED)}").fetchall()
    by_prefix: dict[str, list[tuple[str, str]]] = {}
    by_stem: dict[str, list[tuple[str, str]]] = {}
    for r in rows:
        h, fn = r["doc_hash"], r["file_name"] or ""
        by_prefix.setdefault(h[:8], []).append((h, fn))
        by_stem.setdefault(_norm(Path(fn).stem), []).append((h, fn))

    root = Path(md_root)
    root_r = root.resolve()
    paths: list[Path] = []
    outside_root = 0
    if root.exists():
        for p in sorted(root.rglob("*.md")):
            # md 是产物目录：软链/junction 指向语料外时，正文与身份都不该进库（越界读取）
            try:
                if not p.resolve().is_relative_to(root_r):
                    outside_root += 1
                    continue
            except OSError:
                outside_root += 1
                continue
            paths.append(p)
    hashed_sibling: dict[tuple[Path, str], Path] = {}
    for p in paths:
        if HASH_SUFFIX.search(p.stem):
            hashed_sibling[(p.parent, _norm(base_stem(p.stem)))] = p
    stats = {"md_files": len(paths), "by_hash8": 0, "by_stem": 0, "ambiguous": 0, "orphans": 0,
             "outside_root": outside_root}
    conflicts, orphans_list, old_siblings = [], [], []
    for p in paths:
        rel = p.relative_to(md_root).as_posix()
        stem = p.stem
        hit = HASH_SUFFIX.search(stem)
        if not hit and (p.parent, _norm(base_stem(stem))) in hashed_sibling:
            # 旧 `x.md` 与 reconvert 的 `x_<hash8>.md` 同目录共存：旧件留档但不进题池
            old_siblings.append({"md": rel,
                                 "kept": hashed_sibling[(p.parent, _norm(base_stem(stem)))].name})
            m.products.append({"rel": rel, "path": p, "hash_suffix": False,
                               "doc_hash": None, "size": p.stat().st_size, "selected": False})
            continue
        if hit:
            cands = by_prefix.get(hit.group(1), [])
            how = "hash8"
        else:
            cands = by_stem.get(_norm(base_stem(stem)), [])
            how = "stem"

        if len(cands) > 1:
            # 候选 #36：同 stem 多源不再按文件名猜归属——只有产物字节与某个候选源
            # 完全一致（sha256 前 16 位 == doc_hash）才归属该源；否则记冲突并留孤儿，
            # 各源回退抽自己的源文件，产物以 md-mirror 身份单独成池，内容不丢失。
            try:
                content_hash = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
            except OSError:
                content_hash = None
            exact = [c for c in cands if c[0] == content_hash]
            if len(exact) == 1:
                doc_hash = exact[0][0]
                stats[{"hash8": "by_hash8", "stem": "by_stem"}[how]] += 1
                conflicts.append({"md": rel, "how": how, "candidates": [c[0] for c in cands],
                                  "chosen": doc_hash, "rule": "content_hash_match"})
            else:
                conflicts.append({"md": rel, "how": how, "candidates": [c[0] for c in cands],
                                  "chosen": None, "rule": "unresolved_kept_orphan"})
                stats["ambiguous"] += 1
                stats["orphans"] += 1
                mh = mirror_hash(rel)
                m.orphans[mh] = (rel, p)
                orphans_list.append(rel)
                m.products.append({"rel": rel, "path": p, "hash_suffix": bool(hit),
                                   "doc_hash": None, "size": p.stat().st_size,
                                   "selected": False})
                continue
        elif len(cands) == 1:
            doc_hash = cands[0][0]
            stats[{"hash8": "by_hash8", "stem": "by_stem"}[how]] += 1
        else:
            doc_hash = None
            stats["orphans"] += 1
            mh = mirror_hash(rel)
            m.orphans[mh] = (rel, p)
            orphans_list.append(rel)

        m.products.append({"rel": rel, "path": p, "hash_suffix": bool(hit),
                           "doc_hash": doc_hash, "size": p.stat().st_size, "selected": False})

    # ---- 一个源多产物：选主（hash8 > 更大 > rel） ----
    best: dict[str, dict] = {}
    duplicates = []
    for prod in m.products:
        dh = prod["doc_hash"]
        if not dh:
            continue
        cur = best.get(dh)
        if cur is None:
            best[dh] = prod
            continue
        key_new = (prod["hash_suffix"], prod["size"], prod["rel"])
        key_old = (cur["hash_suffix"], cur["size"], cur["rel"])
        keep, drop = (prod, cur) if key_new > key_old else (cur, prod)
        best[dh] = keep
        duplicates.append({"doc_hash": dh, "kept": keep["rel"], "dropped": drop["rel"]})

    for dh, prod in best.items():
        prod["selected"] = True
        m.by_doc[dh] = (prod["rel"], prod["path"])

    m.report = {**stats,
                "docs_classified": len(rows),
                "docs_with_md": len(m.by_doc),
                "docs_without_md": len(rows) - len(m.by_doc),
                "docs_multi_md": len({d["doc_hash"] for d in duplicates}),
                "conflicts": conflicts,
                "duplicates": duplicates + old_siblings,
                "old_siblings_dropped": len(old_siblings),
                "orphans_list": orphans_list,
                "mapping": {dh: rel for dh, (rel, _p) in m.by_doc.items()}}
    return m
