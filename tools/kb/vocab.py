# -*- coding: utf-8 -*-
"""封闭词表（批次三 R2）：打标 topic 的合法取值集合。

来源 meta/topic_rules.yaml 三段（rules / title_only_rules / folder_topics）的
topic 值并集，剔除"未分类"（那是待复核标记不是主题），补"其他"兜底。
yaml 缺失 / PyYAML 不可用 / 内容损坏 → 空元组宽松降级（G6 与 label_one
走旧口径，不因可选依赖崩）。结果 lru_cache——每次打标调用零开销。
"""
from functools import lru_cache
from pathlib import Path

from . import config

OTHER_TOPIC = "其他"          # 词表外兜底主题（label_one 落表前改写 + review_state=pending）
LEGACY_MARKER = "未分类"      # 分类阶段的"无主题"标记，不是合法打标主题


@lru_cache(maxsize=1)
def topic_vocab() -> tuple[str, ...]:
    try:
        import yaml
    except ImportError:
        return ()
    path = Path(config.TOPIC_RULES_FILE)
    if not path.exists():
        return ()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 —— 损坏的 yaml 同样降级，不阻断打标
        return ()
    topics: set[str] = set()
    for r in data.get("rules") or []:
        if r.get("topic"):
            topics.add(str(r["topic"]))
    for r in data.get("title_only_rules") or []:
        if r.get("topic"):
            topics.add(str(r["topic"]))
    for t in (data.get("folder_topics") or {}).values():
        if t:
            topics.add(str(t))
    topics.discard(LEGACY_MARKER)
    topics.add(OTHER_TOPIC)
    return tuple(sorted(topics))
