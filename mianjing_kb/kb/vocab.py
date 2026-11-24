# -*- coding: utf-8 -*-
"""封闭词表（批次三 R2）：打标 topic 的合法取值集合。

来源 meta/topic_rules.yaml 四段（rules / title_only_rules / folder_topics / label_topics）
的 topic 值并集，剔除"未分类"（那是待复核标记不是主题），补"其他"兜底。
`topic_aliases` 段提供"一级组名 → 合法 topic"的归一表（组名不同名时先归一，别整批判词表外）。
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
        path = Path(config.TOPIC_RULES_FILE)
        if not path.exists():
            return ()
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
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
        # 打标专用补充（taxonomy 一级组名等，不参与分类阶段规则）
        for t in data.get("label_topics") or []:
            if str(t).strip():
                topics.add(str(t).strip())
    except Exception:  # noqa: BLE001 —— 缺失/损坏（含结构错误）一律降级，不阻断打标
        return ()
    topics.discard(LEGACY_MARKER)
    topics.add(OTHER_TOPIC)
    return tuple(sorted(topics))


@lru_cache(maxsize=1)
def topic_aliases() -> dict[str, str]:
    """topic 别名表（如 `算法与数据结构`→`算法`）：校验前先归一，避免组名被判词表外。

    只保留目标在合法词表内的映射；yaml 缺失/损坏返回空 dict（宽松降级）。
    """
    try:
        import yaml
        path = Path(config.TOPIC_RULES_FILE)
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = data.get("topic_aliases") or {}
        vocab = set(topic_vocab())
        return {str(k).strip(): str(v).strip() for k, v in raw.items()
                if str(k).strip() and str(v).strip() in vocab}
    except Exception:  # noqa: BLE001
        return {}


@lru_cache(maxsize=1)
def topic_family() -> dict[str, str]:
    """主题族谱（子 topic → 父 topic），只给 G6b 金标比对用，不改落库 topic。

    合法 topic 同时含产品级（RabbitMQ/Redis）与组级（消息队列/缓存与中间件），
    同一题标成哪一级都说得通；字面比对会把这种口径差异记成"标错"。
    yaml 缺失/损坏 → 空表（退回字面比对）。
    """
    try:
        import yaml
        path = Path(config.TOPIC_RULES_FILE)
        if not path.exists():
            return {}
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        raw = data.get("topic_family") or {}
        return {str(k).strip(): str(v).strip() for k, v in raw.items()
                if str(k).strip() and str(v).strip()}
    except Exception:  # noqa: BLE001
        return {}


def topic_family_root(topic: str) -> str:
    """沿族谱走到根（带环保护）；不在族谱里的 topic 返回自身。"""
    fam = topic_family()
    seen = set()
    cur = (topic or "").strip()
    while cur in fam and cur not in seen:
        seen.add(cur)
        cur = fam[cur]
    return cur


@lru_cache(maxsize=1)
def tag_stopwords() -> frozenset[str]:
    """标签维度停用词：topic / 一级组名 / qtype / position。
    这些是**结构化字段的取值**，不该再出现在 tags 里（否则 tags 37.8% 是维度重复，
    既挤占细粒度关键词，又让检索同义膨胀）。实测 726 题 5,403 次标签里 2,044 次属此类。
    """
    from . import taxonomy
    from .labelspec import positions, qtypes

    topics = set(topic_vocab())
    groups = {g["level1"] for g in taxonomy.load()["groups"]}
    # 别名键也是维度写法（如 `求职与行为`→`HR与求职`），不归一就漏剔：
    # 实测生产库残留 343 次维度词，其中 154+94 就是这类别名键旧写法。
    aliases = set(topic_aliases())
    return frozenset(topics | groups | aliases | set(qtypes()) | set(positions())
                     | {LEGACY_MARKER})
