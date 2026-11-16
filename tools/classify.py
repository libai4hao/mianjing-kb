# -*- coding: utf-8 -*-
"""classify：先分类，再处理（docs/03 §10.4）。

只写元数据，不碰 md 产物 —— 所以改分类的代价是"重跑拆题"，不是"重跑 OCR"。

归因三档（docs/03 §8.4）：
  ① 规则：文件名 regex + 目录结构位 + 顶层主题目录   ← 本脚本实现
  ② 近邻：与已入库文档做 top-20 加权投票             ← 需要向量索引，后续补
  ③ LLM：只处理残渣                                 ← 需要模型服务，后续补

用法：
    python tools/classify.py --stats            # 只看库内分布
    python tools/classify.py                    # 把 pending_classify 全部判掉
    python tools/classify.py --show 40          # 打印前 40 条判定结果，便于抽查
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kb import config, db  # noqa: E402

try:
    import yaml
except ImportError:      # pragma: no cover
    print("[fatal] 需要 PyYAML：pip install pyyaml", file=sys.stderr)
    raise

NOW_FMT = "%Y-%m-%dT%H:%M:%S"


def glob_to_re(pat: str) -> re.Pattern:
    """'*' 只匹配单段，'**' 匹配多段。"""
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif pat[i] == "*":
            out.append("[^/]*")
            i += 1
        else:
            out.append(re.escape(pat[i]))
            i += 1
    return re.compile("^" + "".join(out) + "$")


class Rules:
    def __init__(self):
        topic_doc = yaml.safe_load(config.TOPIC_RULES_FILE.read_text(encoding="utf-8"))
        folder_doc = yaml.safe_load(config.FOLDER_RULES_FILE.read_text(encoding="utf-8"))
        self.title_rules = [(re.compile(r["re"], re.IGNORECASE), r["topic"], r.get("sub_topic", ""))
                            for r in topic_doc.get("rules", [])]
        self.folder_topics = topic_doc.get("folder_topics", {})
        self.structure = [(glob_to_re(r["match"]), r) for r in folder_doc.get("structure", [])]
        self.reject = [glob_to_re(p) for p in folder_doc.get("reject_patterns", [])]
        self.bank_prefixes = tuple(folder_doc.get("top_level_bank_prefixes", []))
        self.ignore_segments = set(folder_doc.get("ignore_segments", []))
        self.defaults = folder_doc.get("defaults", {})

    # ---------- 单文件判定 ----------
    def effective_rel(self, rel: str) -> str:
        """30-补录 是镜像暂存树，判定时剥掉前缀，让目录语义继续生效。"""
        staging = config.STAGING_DIR_NAME + "/"
        return rel[len(staging):] if rel.startswith(staging) else rel

    def category_and_trust(self, rel: str) -> tuple[str, str]:
        for rx, rule in self.structure:
            if rx.match(rel):
                return rule.get("category", config.UNCLASSIFIED), rule.get("source_trust", "medium")
        top = rel.split("/", 1)[0]
        if top.startswith(self.bank_prefixes):
            return "题库", "medium"
        return self.defaults.get("category", config.UNCLASSIFIED), self.defaults.get("source_trust", "medium")

    def topic(self, rel: str) -> tuple[str, str, str]:
        """返回 (topic, sub_topic, topic_source)。"""
        name = rel.rsplit("/", 1)[-1]
        for rx, topic, sub in self.title_rules:
            if rx.search(name):
                return topic, sub, "l1_title"
        top = self.effective_rel(rel).split("/", 1)[0]
        if top in self.folder_topics and self.folder_topics[top] != config.UNCLASSIFIED:
            return self.folder_topics[top], "", "l1_folder"
        return config.UNCLASSIFIED, "", "none"

    def collection(self, rel: str) -> str:
        """来源集 = 目录路径剔除"结构位/包装词/主题名"之后最深的一级。

        剔除主题名（MySQL、JVM、多线程 这类）很关键：否则 "12-综合题库\\Markdown笔记\\MySQL\\"
        会被当成一个"来源集"，把几十份不同出处的资料错绑成一个来源，见证数再次失真。
        若剔除后什么都不剩（说明整条路径都是主题名），退回最深一级，不丢信息。
        """
        eff = self.effective_rel(rel)
        segs = eff.split("/")[:-1]
        kept = [s for s in segs if s not in self.ignore_segments and not self._is_topic_like(s)]
        if kept:
            return kept[-1]
        if segs:
            return segs[-1]
        return "<顶层直属>"

    def _is_topic_like(self, seg: str) -> bool:
        return any(rx.search(seg) for rx, _, _ in self.title_rules)

    def classify(self, rel: str) -> dict:
        eff = self.effective_rel(rel)
        if any(rx.match(eff) for rx in self.reject):
            return {"category": "非内容", "topic": "", "sub_topic": "",
                    "topic_source": "rule_reject", "collection": "", "source_trust": "low"}
        category, trust = self.category_and_trust(eff)
        topic, sub, src = self.topic(eff)
        if topic == config.UNCLASSIFIED and category in ("面经", "真题", "专题笔记"):
            # 面经/真题/课程笔记常常本来就"不限定领域"，这是诚实的答案，不是缺分类；
            # 只有"题库类"没有命中领域时才真的是未分类。
            topic = "综合"
        return {
            "category": category,
            "topic": topic,
            "sub_topic": sub,
            "topic_source": src,
            "collection": self.collection(rel),
            "source_trust": trust,
        }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按规则给源文档打分类")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--status", default="pending_classify", help="要处理的状态，默认 pending_classify")
    ap.add_argument("--redo", action="store_true", help="连已分类的也重判（改规则后用）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="不写库")
    ap.add_argument("--stats", action="store_true", help="只打印分布")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 条判定结果")
    ap.add_argument("--neighbor", action="store_true", help="近邻判类（需向量索引，未实现）")
    args = ap.parse_args(argv)

    if args.neighbor:
        print("[skip] --neighbor 未实现：需要先建文档向量索引（见 docs/03 §8.4 第②档）")

    conn = db.connect(args.db, create=not args.dry_run)
    rules = Rules()

    if args.stats:
        for field in ("status", "category", "topic", "source_trust", "collection"):
            print(f"[{field}] {json.dumps(db.count_by(conn, field), ensure_ascii=False)}")
        conn.close()
        return 0

    where = "status NOT IN ('quarantined','absent','superseded')" if args.redo else f"status='{args.status}'"
    sql = f"SELECT doc_hash, dir_path, file_name FROM source_documents WHERE {where} ORDER BY dir_path"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql).fetchall()
    if not rows:
        print("[info] 没有待分类记录")
        conn.close()
        return 0

    changes = []
    for r in rows:
        rel = f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"]
        res = rules.classify(rel)
        status = "rejected" if res["category"] == "非内容" else "classified"
        changes.append((r["doc_hash"], rel, res))
        if not args.dry_run:
            parent = r["dir_path"].rsplit("/", 1)[-1] if r["dir_path"] else "<顶层直属>"
            conn.execute(
                "UPDATE source_documents SET category=?, topic=?, sub_topic=?, topic_source=?, "
                "collection=?, source_trust=?, parent_dir=?, classifier='rule', classified_at=?, status=? "
                "WHERE doc_hash=?",
                (res["category"], res["topic"], res["sub_topic"], res["topic_source"],
                 res["collection"], res["source_trust"], parent,
                 datetime.now().strftime(NOW_FMT), status, r["doc_hash"]),
            )
    if not args.dry_run:
        conn.commit()

    cat = Counter(c[2]["category"] for c in changes)
    top = Counter(c[2]["topic"] for c in changes)
    src = Counter(c[2]["topic_source"] for c in changes)
    col = Counter(c[2]["collection"] for c in changes)
    print(json.dumps({
        "classified": len(changes),
        "dry_run": args.dry_run,
        "category": dict(cat.most_common()),
        "topic_top10": dict(top.most_common(10)),
        "topic_source": dict(src),
        "collection_top10": dict(col.most_common(10)),
    }, ensure_ascii=False, indent=2))

    if args.show:
        print("\n--- 抽查 ---")
        for doc_hash, rel, res in changes[: args.show]:
            print(f"{res['category']:<4} | {res['topic']:<10} | {res['collection']:<24} | "
                  f"{res['topic_source']:<11} | {rel[:90]}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
