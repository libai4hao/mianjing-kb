# -*- coding: utf-8 -*-
"""全局配置：路径、扩展名白名单、门禁阈值。

所有路径都可以用环境变量覆盖，便于换机器 / 换语料根：
    KB_CORPUS_ROOT   语料根，默认 <CORPUS_ROOT>
    KB_BUILD_DIR     台账与数据库目录，默认 <corpus>\\21-面经数据库\\build
    KB_DB            数据库文件，默认 <build>\\kb.sqlite
"""
from __future__ import annotations

import os
from pathlib import Path

# ---------- 路径 ----------
CORPUS_ROOT = Path(os.environ.get("KB_CORPUS_ROOT", r"<CORPUS_ROOT>"))
BUILD_DIR = Path(os.environ.get("KB_BUILD_DIR", str(CORPUS_ROOT / "21-面经数据库" / "build")))
DB_PATH = Path(os.environ.get("KB_DB", str(BUILD_DIR / "kb.sqlite")))

# 语料树内、但不是"源文档"的目录（产物 / 工作区 / 索引），intake 一律跳过
EXCLUDE_PREFIXES = (
    "00-索引",
    "20-题库MD",
    "21-面经数据库",
)

# 无论出现在哪一层都排除：索引 / 派生树 / 库自身永不当作源文档
# （99-重复文件待删 下也镜像了 20-题库MD 的派生件，不能因为换了位置就被当成新源）
EXCLUDE_DIR_NAMES = ("00-索引", "20-题库MD", "21-面经数据库")

# 隔离区：记录进表但状态标 quarantined，绝不参与后续处理
QUARANTINE_PREFIX = "99-重复文件待删"

# 收件箱：唯一新增入口，路径即语义（见 docs/03 §8）
INBOX_DIR_NAME = "31-收件箱"
STAGING_DIR_NAME = "30-补录"

# 20-题库MD：派生 md 镜像树（源 PDF/docx 转出来的产物，非源文档；
# extract/cluster 把它并入题块池——内容与源件同源，但不重复计频次的"独立来源"）
QUESTION_MD_DIR = CORPUS_ROOT / "20-题库MD"

# ---------- 扩展名 ----------
# 参与入库的"文档型"扩展名
DOC_EXTS = {".pdf", ".docx", ".md", ".txt", ".doc", ".rtf", ".pptx", ".ppt"}
# .doc 是 OLE 复合文档，pymupdf / python-docx 都读不了，必须先转 docx
NEEDS_CONVERT_EXTS = {".doc", ".rtf", ".ppt", ".pptx"}
# 明确不进库的（intake 只计数，不建行；加 --all-exts 时才建行并由 classify 判 rejected）
IGNORED_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".zip", ".rar", ".7z",
                ".xmind", ".xlsx", ".xls", ".mp4", ".mkv", ".exe"}   # P3-8：去掉重复的 .jpg

# ---------- 门禁阈值（v2 §3.3 / R1 修正版）----------
GATE_CJK_MIN = 0.15        # CJK 字符占比下限
GATE_GARBLED_MAX = 0.01    # 乱码字符占比上限
GATE_VALID_MIN = 0.90      # 有效字符占比下限
GATE_MIN_VISIBLE = 800     # 单篇有效字符下限（低于此判 non_content）

# ---------- 分类 ----------
UNCLASSIFIED = "未分类"
META_DIR = Path(os.environ.get("KB_META_DIR", str(Path(__file__).resolve().parents[2] / "meta")))
TOPIC_RULES_FILE = META_DIR / "topic_rules.yaml"
FOLDER_RULES_FILE = META_DIR / "folder_rules.yaml"
# 人工覆盖表（P1-5）：key=语料树内 posix 相对路径，value={category/topic/sub_topic/...}
# 与 docs/03 原方案的 build\overrides.json 不同：这是人工配置不是产物，进 git（meta/）
OVERRIDES_FILE = META_DIR / "overrides.json"
EXCLUSIONS_FILE = META_DIR / "exclusions.json"   # P2-⑤：出题池排除（保留检索、不抽题）

# ---------- OCR ----------
OCR_ENGINE_DIR = Path(os.environ.get(
    "KB_OCR_ENGINE_DIR",
    r"<OCR_ENGINE_DIR>",
))
OCR_ENGINE_EXE = OCR_ENGINE_DIR / "PaddleOCR-json.exe"
OCR_DPI = 200
