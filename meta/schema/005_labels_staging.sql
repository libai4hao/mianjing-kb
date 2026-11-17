-- 005：打标暂存表（docs/15 §3.6——打标结果先进 staging，G6 门禁通过才一次性 merge 进 questions）
-- 一行 = 一道题的 LLM 打标结果；merge 是单事务（UPDATE questions + 清 staging），失败可整批回滚。

CREATE TABLE IF NOT EXISTS labels_staging (
  qid         INTEGER PRIMARY KEY,          -- → questions.qid
  topic       TEXT,                         -- LLM 打标主题（开放集，非空即合格）
  qtype       TEXT,                         -- 八股|算法|HR|场景|项目（G6 枚举校验）
  difficulty  INTEGER,                      -- 1-5（G6 范围校验）
  tags        TEXT,                         -- JSON 数组字符串，如 ["HashMap","扩容"]
  provider    TEXT,                         -- local | online
  model       TEXT,
  redacted    INTEGER NOT NULL DEFAULT 0,   -- 脱敏命中标记（docs/15 §3.7）
  latency_ms  INTEGER,
  tokens      INTEGER,                      -- prompt+completion（预算核算）
  labeled_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ls_labeled_at ON labels_staging(labeled_at);
