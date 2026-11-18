-- 原文档表：唯一事实源（见 docs/03 §10.2）
-- 约定：dir_path 用 posix 分隔符（/)存储，src_path 用系统原生路径。

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS source_documents (
  doc_hash      TEXT PRIMARY KEY,          -- sha256(文件字节) 前 16 位
  sha256        TEXT NOT NULL,             -- 完整 64 位，审计用
  file_name     TEXT NOT NULL,             -- 源文件名（原名，含扩展名）
  ext           TEXT NOT NULL DEFAULT '',
  size_bytes    INTEGER NOT NULL DEFAULT 0,
  src_path      TEXT NOT NULL,             -- 首次发现时的绝对路径
  dir_path      TEXT NOT NULL DEFAULT '',  -- 当前相对语料树路径（posix），移动后更新
  collection    TEXT,                      -- 来源集，见 docs/03 §9.2
  status        TEXT NOT NULL DEFAULT 'pending_classify',
  category      TEXT,                      -- 题库/真题/面经/专题笔记/未分类/非内容
  topic         TEXT,
  sub_topic     TEXT,
  topic_source  TEXT,                      -- l1_folder|l1_title|override|neighbor|llm|none
  classifier    TEXT,                      -- rule|neighbor|llm|manual
  classified_at TEXT,
  source_trust  TEXT,                      -- high|medium|low
  gate_result   TEXT,                      -- pass|low_cjk|garbled|non_content
  family_id     TEXT,                      -- 内容指纹判出的同源族
  batch_id      TEXT NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  note          TEXT,
  -- processing/processed/failed 为预留态，当前无工具写入（2026-11-19 复核确认）；
  -- 动 CHECK 约束需重建表，收益为零，保留枚举仅作扩展位。
  CHECK (status IN (
    'pending_classify','classified','processing','processed',
    'rejected','failed','duplicate','moved','renamed','absent','superseded','quarantined'
  )),
  CHECK (source_trust IS NULL OR source_trust IN ('high','medium','low'))
);

CREATE INDEX IF NOT EXISTS idx_sd_status     ON source_documents(status);
CREATE INDEX IF NOT EXISTS idx_sd_collection ON source_documents(collection);
CREATE INDEX IF NOT EXISTS idx_sd_topic      ON source_documents(topic);
CREATE INDEX IF NOT EXISTS idx_sd_category   ON source_documents(category);
CREATE INDEX IF NOT EXISTS idx_sd_name       ON source_documents(file_name);
CREATE INDEX IF NOT EXISTS idx_sd_family     ON source_documents(family_id);
CREATE INDEX IF NOT EXISTS idx_sd_size       ON source_documents(size_bytes);

-- 每批入库动作的清单，用于回滚与对账（docs/03 §10.5）
CREATE TABLE IF NOT EXISTS batches (
  batch_id    TEXT PRIMARY KEY,
  created_at  TEXT NOT NULL,
  root        TEXT NOT NULL,
  mode        TEXT NOT NULL,               -- full|incremental|meta_only
  added       INTEGER NOT NULL DEFAULT 0,
  duplicated  INTEGER NOT NULL DEFAULT 0,
  moved       INTEGER NOT NULL DEFAULT 0,
  quarantined INTEGER NOT NULL DEFAULT 0,
  ignored     INTEGER NOT NULL DEFAULT 0,
  note        TEXT
);

-- 门禁结果（按 md 产物的路径记账，因为产物与原始文件不是一一对应）
CREATE TABLE IF NOT EXISTS gate_results (
  md_path       TEXT PRIMARY KEY,
  doc_hash      TEXT,
  visible_chars INTEGER NOT NULL DEFAULT 0,
  cjk_chars     INTEGER NOT NULL DEFAULT 0,
  cjk_ratio     REAL NOT NULL DEFAULT 0,
  garbled_ratio REAL NOT NULL DEFAULT 0,
  valid_ratio   REAL NOT NULL DEFAULT 0,
  verdict       TEXT NOT NULL,             -- pass|low_cjk|garbled|non_content
  checked_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_gate_verdict ON gate_results(verdict);

-- OCR 页级记录：一页一行，便于断点续跑与成本核算
CREATE TABLE IF NOT EXISTS ocr_pages (
  doc_hash    TEXT NOT NULL,
  page_no     INTEGER NOT NULL,
  score       REAL,
  text_len    INTEGER NOT NULL DEFAULT 0,
  ocr_at      TEXT NOT NULL,
  PRIMARY KEY (doc_hash, page_no)
);

-- 题目簇（上线后增量吸附用，先建表，不阻塞首轮全量）
CREATE TABLE IF NOT EXISTS canonical_questions (
  canonical_id TEXT PRIMARY KEY,
  title_norm   TEXT NOT NULL,
  topic        TEXT,
  center_vec   BLOB,
  witness      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS question_variants (
  variant_id    TEXT PRIMARY KEY,
  canonical_id  TEXT NOT NULL,
  doc_hash      TEXT NOT NULL,
  question_no   TEXT,
  answering_text TEXT,
  collection    TEXT,
  created_at    TEXT NOT NULL,
  FOREIGN KEY (canonical_id) REFERENCES canonical_questions(canonical_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_var_canonical ON question_variants(canonical_id);
CREATE INDEX IF NOT EXISTS idx_var_doc       ON question_variants(doc_hash);
