-- 008：金标表（批次二 G1）——人工抽检金标，G6b 门禁依据（docs/15 G6：抽检准确率 ≥0.8）。
-- 键 = q_norm_hash（与 label_facts 同，跨 extract 重切存活）；
-- topic_gold 必填，qtype_gold/difficulty_gold 可空（只测 topic 准确率时够用）。
CREATE TABLE IF NOT EXISTS golden_labels (
  q_norm_hash     TEXT PRIMARY KEY,
  q_text          TEXT NOT NULL,
  topic_gold      TEXT NOT NULL,
  qtype_gold      TEXT,
  difficulty_gold INTEGER,
  annotator       TEXT NOT NULL DEFAULT 'human',
  created_at      TEXT NOT NULL
);
