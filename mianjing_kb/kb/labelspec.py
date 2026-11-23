# -*- coding: utf-8 -*-
"""标签维度定义（T21）：`meta/label_schema.yaml` 的读取 + 校验 + 标签归一。

单一来源：prompt 文案、`label_one` 落表校验、G6 门禁都读这里，避免
"prompt 说一套、G6 查另一套"（qtype 枚举曾在 batch_label / verify_ledger 各写一份）。

降级：yaml 缺失 / PyYAML 不可用 / 结构错误 → 用模块内 DEFAULT_SCHEMA，不阻断打标。
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from . import config

DEFAULT_SCHEMA = {
    "version": 1,
    "fields": {
        "topic": {"kind": "enum", "source": "topic_vocab", "required": True, "max_len": 40},
        "qtype": {"kind": "enum", "values": ["八股", "算法", "HR", "场景", "项目"], "required": True},
        "difficulty": {"kind": "int", "min": 1, "max": 5, "required": True},
        "position": {"kind": "enum_multi",
                     "values": ["后端", "前端", "算法", "测试", "客户端", "数据", "其他"],
                     "max_items": 2, "required": False},
        "quality": {"kind": "enum_multi",
                    "values": ["重复", "残缺", "过时", "广告", "无答案"],
                    "max_items": 3, "required": False},
        "canonical_q": {"kind": "text", "max_len": 60, "required": True},
        "tags": {"kind": "open_tags", "min_items": 0, "max_items": 8, "max_len": 24},
    },
}


@lru_cache(maxsize=1)
def load_schema() -> dict:
    """读 label_schema.yaml；任何异常都退回 DEFAULT_SCHEMA（深拷贝语义由只读使用保证）。"""
    try:
        import yaml
        path = Path(config.LABEL_SCHEMA_FILE)
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        fields = data.get("fields")
        if not isinstance(fields, dict) or not fields:
            return DEFAULT_SCHEMA
        merged = {**DEFAULT_SCHEMA["fields"]}
        for k, v in fields.items():
            if isinstance(v, dict):
                merged[k] = {**DEFAULT_SCHEMA["fields"].get(k, {}), **v}
        return {"version": data.get("version", 1), "fields": merged}
    except Exception:  # noqa: BLE001 —— 可选依赖/配置缺失不阻断打标
        return DEFAULT_SCHEMA


def field(name: str) -> dict:
    return load_schema()["fields"].get(name, {})


def qtypes() -> tuple[str, ...]:
    return tuple(field("qtype").get("values") or ())


def positions() -> tuple[str, ...]:
    return tuple(field("position").get("values") or ())


def quality_flags() -> tuple[str, ...]:
    return tuple(field("quality").get("values") or ())


def canonical_max_len() -> int:
    return int(field("canonical_q").get("max_len") or 60)


def difficulty_range() -> tuple[int, int]:
    return int(field("difficulty").get("min") or 1), int(field("difficulty").get("max") or 5)


def tag_limits() -> tuple[int, int]:
    """(max_items, max_len)。"""
    f = field("tags")
    return int(f.get("max_items") or 8), int(f.get("max_len") or 24)


@lru_cache(maxsize=1)
def tag_synonyms() -> dict[str, str]:
    """别名（小写）→ 规范标签；文件缺失返回空表。"""
    try:
        import yaml
        path = Path(config.TAG_SYNONYMS_FILE)
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return {str(k).strip().lower(): str(v).strip() for k, v in data.items()
                if str(k).strip() and str(v).strip()}
    except Exception:  # noqa: BLE001
        return {}


@lru_cache(maxsize=1)
def recommended_tags() -> tuple[str, ...]:
    """二级推荐词表（tag_vocab.yaml 各组并集）；仅用于 prompt，不强制封闭。"""
    try:
        import yaml
        path = Path(config.TAG_VOCAB_FILE)
        if not path.exists():
            return ()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        buckets = [list(items or []) for items in (data.get("groups") or {}).values()]
        tags: list[str] = []
        while any(buckets):                    # 各组轮转：prompt 截断时也覆盖所有领域
            for b in buckets:
                if b:
                    t = str(b.pop(0)).strip()
                    if t:
                        tags.append(t)
        return tuple(dict.fromkeys(tags))
    except Exception:  # noqa: BLE001
        return ()


def normalize_tags(raw, *, max_items: int | None = None, max_len: int | None = None,
                   synonyms: dict[str, str] | None = None,
                   stopwords: frozenset[str] | set[str] | None = None
                   ) -> tuple[list[str], int, int]:
    """标签归一：去空白/同义词映射/去重/去维度词/截断。

    返回 (规范标签列表, 命中同义词的个数, 被当维度词剔除的个数)。

    `synonyms` 可由调用方传入（batch_label 用数据库配置表的 aliases）；
    不传则回退 `meta/tag_synonyms.yaml`。归一目标是"合并同义写法"，不是"只允许词表内标签"。
    `stopwords` 是结构化字段取值（topic/组名/qtype/position），tags 里出现即剔除——
    实测这类重复占 37.8% 的标签出现量，留着只挤占细粒度关键词。
    """
    cap_items, cap_len = tag_limits()
    cap_items = max_items or cap_items
    cap_len = max_len or cap_len
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return [], 0, 0
    syn = tag_synonyms() if synonyms is None else synonyms
    stop = set(stopwords or ())
    out: list[str] = []
    hits = 0
    dropped = 0
    seen: set[str] = set()
    for item in raw:
        tag = str(item or "").strip()
        if not tag:
            continue
        canonical = syn.get(tag.lower())
        if canonical and canonical != tag:
            hits += 1
            tag = canonical
        tag = tag[:cap_len]
        if tag in stop:
            dropped += 1
            continue
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(tag)
        if len(out) >= cap_items:
            break
    return out, hits, dropped


def validate_label(obj: dict, vocab: tuple[str, ...] = (), q_text: str = "", *,
                   synonyms: dict[str, str] | None = None,
                   topic_aliases: dict[str, str] | None = None,
                   tag_stopwords: frozenset[str] | set[str] | None = None) -> dict:
    """LLM 输出 → 规范化字段 + issues + review_state。

    非法枚举（qtype / difficulty）直接抛 ValueError，让调用方落 failed 队列重试；
    topic 词表外、position/quality 越界属"可降级"——改写/丢弃并标 review_state=pending。
    `topic_aliases`（一级组名→合法 topic）在校验前先归一，归一命中不算词表外。
    """
    from .vocab import OTHER_TOPIC

    out: dict = {"issues": [], "review_state": None}
    topic = str(obj.get("topic") or "").strip()[: int(field("topic").get("max_len") or 40)]
    if topic_aliases and topic in topic_aliases:
        topic = topic_aliases[topic]
        out["issues"].append("topic_alias_normalized")
    if vocab and topic not in vocab:
        raw_topic = topic
        topic = OTHER_TOPIC
        # 带上模型原话，人工终审才知道该归哪个桶（否则 pending 行只剩一个"其他"）
        out["issues"].append(f"topic_out_of_vocab:{raw_topic}")
        out["review_state"] = "pending"
    out["topic"] = topic

    qt = obj.get("qtype")
    if qt not in qtypes():
        raise ValueError(f"qtype 非法: {qt!r}（允许 {','.join(qtypes())}）")
    out["qtype"] = qt

    lo, hi = difficulty_range()
    try:
        diff = int(obj.get("difficulty"))
    except (TypeError, ValueError):
        raise ValueError(f"difficulty 非法: {obj.get('difficulty')!r}（允许 {lo}-{hi} 整数）")
    if not lo <= diff <= hi:
        raise ValueError(f"difficulty 越界: {diff}（允许 {lo}-{hi}）")
    out["difficulty"] = diff

    for name, allowed, cap in (("position", positions(), int(field("position").get("max_items") or 2)),
                              ("quality", quality_flags(), int(field("quality").get("max_items") or 3))):
        raw = obj.get(name)
        if raw is None or raw == "":
            out[name] = None
            continue
        if isinstance(raw, str):
            raw = [raw]
        vals = []
        for v in (raw if isinstance(raw, (list, tuple)) else []):
            s = str(v).strip()
            if not s:
                continue
            if s in allowed and s not in vals:
                vals.append(s)
        dropped = [str(v) for v in (raw if isinstance(raw, (list, tuple)) else []) if str(v).strip() not in allowed]
        if dropped:
            out["issues"].append(f"{name}_invalid")
            out["review_state"] = out["review_state"] or "pending"
        out[name] = vals[:cap] or None

    canonical = str(obj.get("canonical_q") or "").strip()[: canonical_max_len()]
    if not canonical:
        canonical = (q_text or "").strip().replace("\n", " ")[: canonical_max_len()]
    out["canonical_q"] = canonical

    tags, hits, dropped = normalize_tags(obj.get("tags") or [], synonyms=synonyms,
                                         stopwords=tag_stopwords)
    out["tags"] = tags
    out["tag_norm_hits"] = hits
    out["tag_dim_dropped"] = dropped
    if dropped:
        out["issues"].append(f"tag_dimension_dropped:{dropped}")
    return out


def dump_multi(values: list[str] | None) -> str | None:
    """多选列落库格式：JSON 数组字符串；空 → None（merge 时 NULLIF 不覆盖旧值）。"""
    return json.dumps(values, ensure_ascii=False) if values else None
