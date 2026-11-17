-- 004：题目表（P1-1 extract 落表，chunker v2 产出）
-- 一行 = 一篇源文档切出的一个题块；q_norm/simhash 支撑同题频次统计（"同一个题目统计出现频率"）。

CREATE TABLE IF NOT EXISTS questions (
  qid         INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_hash    TEXT NOT NULL,             -- → source_documents.doc_hash
  seq         INTEGER NOT NULL,          -- 文档内序号（chunk_v2 items.seq）
  q_text      TEXT NOT NULL,             -- 题干原文（chunk_v2 items.q）
  a_text      TEXT NOT NULL DEFAULT '',  -- 答案原文（items.a）
  span_start  INTEGER NOT NULL,          -- 相对抽取文本的字符偏移
  span_end    INTEGER NOT NULL,
  qtype       TEXT NOT NULL DEFAULT '八股',  -- 八股|算法|项目|场景|HR
  conf        TEXT NOT NULL DEFAULT 'low',   -- high|low|none
  q_norm      TEXT NOT NULL,             -- question_freq.normalize(q_text)
  q_norm_hash TEXT NOT NULL,             -- sha256(q_norm) 前 16 位，频次分组键
  simhash     INTEGER NOT NULL,          -- 近重复召回
  extracted_at TEXT NOT NULL,
  UNIQUE (doc_hash, seq)
);

CREATE INDEX IF NOT EXISTS idx_q_doc   ON questions(doc_hash);
CREATE INDEX IF NOT EXISTS idx_q_norm  ON questions(q_norm_hash);
CREATE INDEX IF NOT EXISTS idx_q_qtype ON questions(qtype);

-- 同题频次视图：精确同文（q_norm_hash）直接聚合，近重复簇留给 P1-4 report 用 simhash 处理
CREATE VIEW IF NOT EXISTS question_freq AS
SELECT q_norm_hash,
       MIN(q_norm)                        AS sample_q,
       COUNT(*)                           AS n_docs,
       COUNT(DISTINCT doc_hash)           AS n_distinct_docs,
       GROUP_CONCAT(DISTINCT doc_hash)    AS doc_hashes
FROM questions
GROUP BY q_norm_hash
HAVING n_distinct_docs >= 2
ORDER BY n_distinct_docs DESC;
