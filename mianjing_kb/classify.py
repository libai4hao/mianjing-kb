# -*- coding: utf-8 -*-
"""classify：先分类，再处理（docs/03 §10.4）。

只写元数据，不碰 md 产物 —— 所以改分类的代价是"重跑拆题"，不是"重跑 OCR"。

归因三档（docs/03 §8.4）：
  ① 规则：文件名 regex + 目录结构位 + 顶层主题目录   ← 本脚本实现
  ② 近邻：与已入库文档做 top-20 加权投票             ← 需要向量索引，后续补
  ③ LLM：只处理残渣                                 ← 需要模型服务，后续补

用法：
    python -m mianjing_kb.classify --stats            # 只看库内分布
    python -m mianjing_kb.classify                    # 把 pending_classify 全部判掉
    python -m mianjing_kb.classify --show 40          # 打印前 40 条判定结果，便于抽查
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
from mianjing_kb.kb import config, db  # noqa: E402
from mianjing_kb.kb import status as st  # noqa: E402  （本模块有同名局部变量/参数 status，用别名避免遮蔽）

try:
    import yaml
except ImportError:      # pragma: no cover
    print("[fatal] 需要 PyYAML：pip install pyyaml", file=sys.stderr)
    raise

NOW_FMT = "%Y-%m-%dT%H:%M:%S"

# --status 白名单（P1 注入面修复）：状态全集只在 kb/status.py 定义，这里只做引用
VALID_STATUSES = frozenset(st.ALL)


def build_select_sql(status: str, redo: bool, limit: int) -> tuple[str, list]:
    """构造待分类记录的 SELECT（P1 修复：状态参数化 + 白名单，杜绝命令行注入）。

    返回 (sql, params)。--redo 走固定 NOT IN 子句、忽略 status；limit 强转非负整数。
    """
    if redo:
        # 'processed' 不进重判范围：它表示"已入库有产物"，重判会被打回 classified、
        # 把入库状态抹掉（2026-11-19 审核修复）。确需重判单件走 --status processed。
        where = "status NOT IN " + st.sql_in(
            (st.QUARANTINED, st.ABSENT, st.SUPERSEDED, st.PROCESSED))
        params: list = []
    else:
        if status not in VALID_STATUSES:
            raise ValueError(f"非法状态 {status!r}，允许: {sorted(VALID_STATUSES)}")
        where = "status=?"
        params = [status]
    sql = f"SELECT doc_hash, dir_path, file_name FROM source_documents WHERE {where} ORDER BY dir_path"
    if limit:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValueError(f"非法 limit {limit!r}，须为非负整数")
        sql += f" LIMIT {limit}"
    return sql, params


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
    # 目录名性质词 → (类别, 可信度)：先判面经，再笔试，最后面试（"面试技术岗"目录别被笔试题抢走）
    DIR_HINTS = (("面经", "面经", "low"), ("笔试", "真题", "high"), ("面试", "题库", "medium"))

    def __init__(self):
        topic_doc = yaml.safe_load(config.TOPIC_RULES_FILE.read_text(encoding="utf-8"))
        folder_doc = yaml.safe_load(config.FOLDER_RULES_FILE.read_text(encoding="utf-8"))
        self.title_rules = [(re.compile(r["re"], re.IGNORECASE), r["topic"], r.get("sub_topic", ""))
                            for r in topic_doc.get("rules", [])]
        self.title_only_rules = [(re.compile(r["re"], re.IGNORECASE), r["topic"], r.get("sub_topic", ""))
                                 for r in topic_doc.get("title_only_rules", [])]
        self.folder_topics = topic_doc.get("folder_topics", {})
        self.structure = [(glob_to_re(r["match"]), r) for r in folder_doc.get("structure", [])]
        self.reject = [glob_to_re(p) for p in folder_doc.get("reject_patterns", [])]
        self.bank_prefixes = tuple(folder_doc.get("top_level_bank_prefixes", []))
        self.ignore_segments = set(folder_doc.get("ignore_segments", []))
        self.defaults = folder_doc.get("defaults", {})
        # 人工覆盖表（P1-5）：优先级最高，key 支持正反斜杠（统一成 posix 比对）
        self.overrides: dict[str, dict] = {}
        if config.OVERRIDES_FILE.exists():
            raw = json.loads(config.OVERRIDES_FILE.read_text(encoding="utf-8"))
            self.overrides = {k.replace("\\", "/"): v for k, v in raw.items()}

    # ---------- 单文件判定 ----------
    def effective_rel(self, rel: str) -> str:
        """30-补录 是镜像暂存树，判定时剥掉前缀，让目录语义继续生效。"""
        staging = config.STAGING_DIR_NAME + "/"
        return rel[len(staging):] if rel.startswith(staging) else rel

    def category_and_trust(self, rel: str) -> tuple[str, str]:
        for rx, rule in self.structure:
            # 兜底规则（31-收件箱/** → 未分类）不算命中：留给目录名性质词兜底，
            # 否则收件箱里凡是没细分到三类子目录的件就永远停在未分类。
            if rx.match(rel) and rule.get("category", config.UNCLASSIFIED) != config.UNCLASSIFIED:
                return rule.get("category", config.UNCLASSIFIED), rule.get("source_trust", "medium")
        hint = self.dir_hint(rel)
        if hint:
            return hint
        top = rel.split("/", 1)[0]
        if top.startswith(self.bank_prefixes):
            return "题库", "medium"
        return self.defaults.get("category", config.UNCLASSIFIED), self.defaults.get("source_trust", "medium")

    def dir_hint(self, rel: str) -> tuple[str, str] | None:
        """目录名里的"性质词"兜底：笔试题目录 → 真题 / 面经目录 → 面经 / 面试题目录 → 题库。

        只在这几条通用词上判，且从最深一级往上找；比文件名判类稳（文件名常只有岗位与轮次）。
        """
        for seg in reversed(rel.split("/")[:-1]):
            for word, cat, trust in self.DIR_HINTS:
                if word in seg:
                    return cat, trust
        return None

    def topic(self, rel: str) -> tuple[str, str, str]:
        """返回 (topic, sub_topic, topic_source)。"""
        name = rel.rsplit("/", 1)[-1]
        for rx, topic, sub in self.title_rules:
            if rx.search(name):
                return topic, sub, "l1_title"
        for rx, topic, sub in self.title_only_rules:
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
        # P1-5：overrides 最高优先级——人工拍板的 category/topic 直接生效
        for key in (rel.replace("\\", "/"), eff):
            if key in self.overrides:
                ov = dict(self.overrides[key])
                base = {"category": ov.get("category", config.UNCLASSIFIED),
                        "topic": ov.get("topic", ""),
                        "sub_topic": ov.get("sub_topic", ""),
                        "topic_source": "override",
                        "collection": self.collection(rel),
                        "source_trust": ov.get("source_trust", "high"),
                        "note": ov.get("note", "overrides.json 人工覆盖")}
                return base
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


def _title_norm(name: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", name.rsplit(".", 1)[0].lower())


def _grams(s: str) -> set:
    return {s[i:i + 2] for i in range(len(s) - 1)} if len(s) >= 2 else ({s} if s else set())


def neighbor_vote(target_name: str, category: str,
                  refs: list[tuple[str, str, str]], top_k: int = 5,
                  min_jac: float = 0.45, min_conf: float = 0.6) -> dict:
    """近邻判类（归因②档）：与同类目已入库文档做标题 2-gram Jaccard top-k 加权投票。

    refs = [(file_name, topic, sub_topic)]；只投非"综合"领域。
    返回 {"topic","sub_topic","confidence","best_jac","neighbors":[...]}；不达线 topic=未分类。
    """
    tg = _grams(_title_norm(target_name))
    scored = []
    for fname, topic, sub in refs:
        if not topic or topic == "综合":
            continue
        j = jaccard_local(tg, _grams(_title_norm(fname)))
        if j >= min_jac:
            scored.append((j, topic, sub or "", fname))
    if not scored:
        return {"topic": config.UNCLASSIFIED, "sub_topic": "", "confidence": 0.0,
                "best_jac": 0.0, "neighbors": []}
    scored.sort(key=lambda x: -x[0])
    top = scored[:top_k]
    votes: dict[str, float] = {}
    sub_of: dict[str, str] = {}
    for j, topic, sub, _ in top:
        votes[topic] = votes.get(topic, 0.0) + j
        sub_of.setdefault(topic, sub)
    total = sum(votes.values())
    topic, w = max(votes.items(), key=lambda kv: kv[1])
    conf = w / total if total else 0.0
    if conf < min_conf:
        return {"topic": config.UNCLASSIFIED, "sub_topic": "", "confidence": round(conf, 3),
                "best_jac": top[0][0], "neighbors": [(f, t, round(j, 3)) for j, t, s, f in top]}
    return {"topic": topic, "sub_topic": sub_of.get(topic, ""), "confidence": round(conf, 3),
            "best_jac": top[0][0], "neighbors": [(f, t, round(j, 3)) for j, t, s, f in top]}


def jaccard_local(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def run_neighbor(conn, rules, dry_run: bool = False, show: int = 0) -> int:
    """归因②档：topic=未分类 的已入库件 ← 同类目已入库件标题近邻投票。

    置信 ≥0.6 自动应用（topic_source='neighbor'）；不达线写 build/topic_neighbor_review.csv 待人工。
    """
    import csv as _csv
    uncls = conn.execute(
        "SELECT doc_hash, dir_path, file_name, category FROM source_documents "
        f"WHERE status='{st.CLASSIFIED}' AND topic=? ORDER BY dir_path", (config.UNCLASSIFIED,)).fetchall()
    refs = conn.execute(
        "SELECT file_name, topic, sub_topic, category FROM source_documents "
        f"WHERE status='{st.CLASSIFIED}' AND topic NOT IN (?, '')",
        (config.UNCLASSIFIED,)).fetchall()
    ref_by_cat: dict[str, list] = {}
    for r in refs:
        ref_by_cat.setdefault(r["category"], []).append((r["file_name"], r["topic"], r["sub_topic"]))
    print(f"[neighbor] 未分类 {len(uncls)} 件，参照池 "
          f"{ {k: len(v) for k, v in ref_by_cat.items()} }", flush=True)

    applied, review = [], []
    for r in uncls:
        res = neighbor_vote(r["file_name"], r["category"],
                            ref_by_cat.get(r["category"], []))
        rec = {"dir_path": r["dir_path"], "file_name": r["file_name"], "category": r["category"],
               "proposal_topic": res["topic"], "sub_topic": res["sub_topic"],
               "confidence": res["confidence"], "best_jac": res["best_jac"],
               "neighbors": "; ".join(f"{f}→{t}({j})" for f, t, j in res["neighbors"][:3])}
        # 只自动应用"同题双格式"级命中（jac≥0.9 且置信≥0.6）；
        # 0.45~0.9 的提案仅入待人工清单——实测"技术岗面试"会被投给"非技术岗面试"(0.8)，
        # 字面子串陷阱，不许自动落库。
        if res["topic"] != config.UNCLASSIFIED and res["best_jac"] >= 0.9 and res["confidence"] >= 0.6:
            applied.append((r["doc_hash"], r, res, rec))
        else:
            rec["proposal_topic"] = f"<待人工:{res['topic']}>" if res["topic"] != config.UNCLASSIFIED else "<待人工>"
            review.append(rec)

    if not dry_run:
        from datetime import datetime
        for doc_hash, r, res, _rec in applied:
            conn.execute(
                "UPDATE source_documents SET topic=?, sub_topic=?, topic_source='neighbor', "
                "classified_at=? WHERE doc_hash=?",
                (res["topic"], res["sub_topic"], datetime.now().strftime(NOW_FMT), doc_hash))
        conn.commit()

    rp = config.BUILD_DIR / "topic_neighbor_review.csv"
    rp.parent.mkdir(parents=True, exist_ok=True)
    with rp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["dir_path", "file_name", "category", "proposal_topic",
                                            "sub_topic", "confidence", "best_jac", "neighbors"])
        w.writeheader()
        for _, _, _, rec in applied:
            w.writerow(rec)
        for rec in review:
            w.writerow(rec)

    from collections import Counter
    top = Counter(res["topic"] for _, _, res, _ in applied)
    print(json.dumps({
        "unclassified": len(uncls),
        "auto_applied": len(applied) if not dry_run else 0,
        "would_apply": len(applied),
        "topic_dist": dict(top.most_common()),
        "review_left": len(review),
        "dry_run": dry_run,
    }, ensure_ascii=False, indent=1))
    print(f"[review] {len(review)} 件待人工 → {rp}")
    if show:
        print("\n--- 自动应用抽样 ---")
        for doc_hash, r, res, _rec in applied[:show]:
            print(f"  {res['topic']:<8} conf={res['confidence']:<6} jac={res['best_jac']:<6} {r['file_name'][:70]}")
    return 0


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def vote_semantic(scored: list[tuple], top_k: int = 5, min_conf: float = 0.6) -> dict:
    """语义近邻投票（纯函数，可离线单测）：scored = [(cos, topic, sub_topic, file_name)]，
    调用方保证已按 cos 降序。返回与 neighbor_vote 同构。"""
    top = scored[:top_k]
    if not top:
        return {"topic": config.UNCLASSIFIED, "sub_topic": "", "confidence": 0.0,
                "best_cos": 0.0, "neighbors": []}
    votes: dict[str, float] = {}
    sub_of: dict[str, str] = {}
    for cos_, topic, sub, _ in top:
        votes[topic] = votes.get(topic, 0.0) + cos_
        sub_of.setdefault(topic, sub)
    total = sum(votes.values())
    topic, w = max(votes.items(), key=lambda kv: kv[1])
    conf = w / total if total else 0.0
    if conf < min_conf:
        return {"topic": config.UNCLASSIFIED, "sub_topic": "", "confidence": round(conf, 3),
                "best_cos": round(top[0][0], 3),
                "neighbors": [(f, t, round(c, 3)) for c, t, s, f in top]}
    return {"topic": topic, "sub_topic": sub_of.get(topic, ""), "confidence": round(conf, 3),
            "best_cos": round(top[0][0], 3),
            "neighbors": [(f, t, round(c, 3)) for c, t, s, f in top]}


def run_neighbor_vec(conn, dry_run: bool = False, show: int = 0) -> int:
    """归因②档语义版（docs/03 §8.4「需要向量索引」一项，2026-11-17 落地）：
    fastembed(bge-small-zh-v1.5) 嵌入标题 → 余弦 top-5 加权投票。

    与 run_neighbor（标题 2-gram Jaccard）互补：命中"多线程↔并发"这类同义改写。
    自动落库门槛：best_cos≥0.85 且 conf≥0.6（余弦分布整体偏高，0.85 ≈ Jaccard 0.9
    的保守度，宁缺勿滥——错分类比缺分类难清）；其余出待人工清单。
    """
    import csv as _csv
    from mianjing_kb.embed_questions import embed_texts  # 惰性加载模型（fastembed 首跑下载）

    uncls = conn.execute(
        "SELECT doc_hash, dir_path, file_name, category FROM source_documents "
        f"WHERE status='{st.CLASSIFIED}' AND topic=? ORDER BY dir_path", (config.UNCLASSIFIED,)).fetchall()
    refs = conn.execute(
        "SELECT doc_hash, file_name, topic, sub_topic, category FROM source_documents "
        f"WHERE status='{st.CLASSIFIED}' AND topic NOT IN (?, '', '综合')",
        (config.UNCLASSIFIED,)).fetchall()
    ref_by_cat: dict[str, list] = {}
    for r in refs:
        ref_by_cat.setdefault(r["category"], []).append(r)
    print(f"[neighbor-vec] 未分类 {len(uncls)} 件，参照池 "
          f"{ {k: len(v) for k, v in ref_by_cat.items()} }", flush=True)

    # 批量嵌入（一次模型调用；标题去扩展名）
    stems_ref = [Path(r["file_name"]).stem for r in refs]
    stems_tgt = [Path(r["file_name"]).stem for r in uncls]
    vecs = embed_texts(stems_ref + stems_tgt)
    vec_of = dict(zip(stems_ref + stems_tgt, vecs))

    applied, review = [], []
    for r in uncls:
        tv = vec_of.get(Path(r["file_name"]).stem)
        scored = []
        for ref in ref_by_cat.get(r["category"], []):
            rv = vec_of.get(Path(ref["file_name"]).stem)
            if not tv or not rv:
                continue
            c = _cos(tv, rv)
            if c >= 0.5:
                scored.append((c, ref["topic"], ref["sub_topic"] or "", ref["file_name"]))
        scored.sort(key=lambda x: -x[0])
        res = vote_semantic(scored)
        rec = {"dir_path": r["dir_path"], "file_name": r["file_name"], "category": r["category"],
               "proposal_topic": res["topic"], "sub_topic": res["sub_topic"],
               "confidence": res["confidence"], "best_cos": res["best_cos"],
               "neighbors": "; ".join(f"{f}→{t}({c})" for f, t, c in res["neighbors"][:3])}
        if res["topic"] != config.UNCLASSIFIED and res["best_cos"] >= 0.85 and res["confidence"] >= 0.6:
            applied.append((r["doc_hash"], r, res, rec))
        else:
            rec["proposal_topic"] = f"<待人工:{res['topic']}>" if res["topic"] != config.UNCLASSIFIED else "<待人工>"
            review.append(rec)

    if not dry_run:
        for doc_hash, r, res, _rec in applied:
            conn.execute(
                "UPDATE source_documents SET topic=?, sub_topic=?, topic_source='neighbor', "
                "classifier='neighbor', classified_at=? WHERE doc_hash=?",
                (res["topic"], res["sub_topic"], datetime.now().strftime(NOW_FMT), doc_hash))
        conn.commit()

    rp = config.BUILD_DIR / "topic_neighbor_vec_review.csv"
    rp.parent.mkdir(parents=True, exist_ok=True)
    with rp.open("w", encoding="utf-8-sig", newline="") as fh:
        w = _csv.DictWriter(fh, fieldnames=["dir_path", "file_name", "category", "proposal_topic",
                                            "sub_topic", "confidence", "best_cos", "neighbors"])
        w.writeheader()
        for _, _, _, rec in applied:
            w.writerow(rec)
        for rec in review:
            w.writerow(rec)

    from collections import Counter
    top = Counter(res["topic"] for _, _, res, _ in applied)
    print(json.dumps({
        "unclassified": len(uncls),
        "auto_applied": len(applied) if not dry_run else 0,
        "would_apply": len(applied),
        "topic_dist": dict(top.most_common()),
        "review_left": len(review),
        "dry_run": dry_run,
    }, ensure_ascii=False, indent=1))
    print(f"[review] {len(review)} 件待人工 → {rp}")
    if show:
        print("\n--- 自动应用抽样 ---")
        for doc_hash, r, res, _rec in applied[:show]:
            print(f"  {res['topic']:<8} conf={res['confidence']:<6} cos={res['best_cos']:<6} {r['file_name'][:70]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按规则给源文档打分类")
    ap.add_argument("--db", default=str(config.DB_PATH))
    ap.add_argument("--status", default=st.PENDING, help="要处理的状态，默认 pending_classify")
    ap.add_argument("--redo", action="store_true", help="连已分类的也重判（改规则后用）")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true", help="不写库")
    ap.add_argument("--stats", action="store_true", help="只打印分布")
    ap.add_argument("--show", type=int, default=0, help="打印前 N 条判定结果")
    ap.add_argument("--neighbor", action="store_true",
                    help="近邻判类：给 topic=未分类 的已入库件做标题近邻投票（自动应用 ≥0.6 置信，其余出待人工清单）")
    ap.add_argument("--vec", action="store_true",
                    help="配合 --neighbor：改用语义向量（fastembed 标题余弦，命中同义改写），门槛 cos≥0.85+conf≥0.6")
    args = ap.parse_args(argv)

    conn = db.connect(args.db, create=not args.dry_run)
    rules = Rules()

    if args.neighbor:
        if args.vec:
            rc = run_neighbor_vec(conn, dry_run=args.dry_run, show=args.show)
        else:
            rc = run_neighbor(conn, rules, dry_run=args.dry_run, show=args.show)
        conn.close()
        return rc

    if args.stats:
        for field in ("status", "category", "topic", "source_trust", "collection"):
            print(f"[{field}] {json.dumps(db.count_by(conn, field), ensure_ascii=False)}")
        conn.close()
        return 0

    sql, params = build_select_sql(args.status, args.redo, args.limit)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print("[info] 没有待分类记录")
        conn.close()
        return 0

    changes = []
    for r in rows:
        rel = f"{r['dir_path']}/{r['file_name']}" if r["dir_path"] else r["file_name"]
        res = rules.classify(rel)
        new_status = st.REJECTED if res["category"] == "非内容" else st.CLASSIFIED
        changes.append((r["doc_hash"], rel, res))
        if not args.dry_run:
            parent = r["dir_path"].rsplit("/", 1)[-1] if r["dir_path"] else "<顶层直属>"
            conn.execute(
                "UPDATE source_documents SET category=?, topic=?, sub_topic=?, topic_source=?, "
                "collection=?, source_trust=?, parent_dir=?, classifier='rule', classified_at=?, status=? "
                "WHERE doc_hash=?",
                (res["category"], res["topic"], res["sub_topic"], res["topic_source"],
                 res["collection"], res["source_trust"], parent,
                 datetime.now().strftime(NOW_FMT), new_status, r["doc_hash"]),
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
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
