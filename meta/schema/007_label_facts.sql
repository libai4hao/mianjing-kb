-- 007：打标事实源（跨 extract 重切存活）——修复审核报告 B1（2026-11-19）
-- merge 时按 q_norm_hash 落档；extract 重切后按 q_norm_hash 重挂。
-- q_norm_hash = sha256(归一化题干)[:16]（tools/extract.py），同文题共享同一 hash：
-- 重挂语义 = 同文题打标一致（canonical 同题簇同理）。
CREATE TABLE IF NOT EXISTS label_facts (
  q_norm_hash TEXT PRIMARY KEY,
  topic       TEXT NOT NULL,
  qtype       TEXT,
  difficulty  INTEGER,
  tags        TEXT,               -- JSON 数组字符串
  merged_at   TEXT NOT NULL
);
