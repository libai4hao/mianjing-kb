-- 009：labels_staging 复核三列（批次三 R1）。
-- review_state：NULL=未复核 / 'pending'=词表外待复核 / 'agree'=复核通过 / 'disagree'=复核存疑；
-- disagree 行 merge 不落库（人工 --review-resolve 终审后放行）。
ALTER TABLE labels_staging ADD COLUMN review_state TEXT;
ALTER TABLE labels_staging ADD COLUMN review_model TEXT;
ALTER TABLE labels_staging ADD COLUMN review_note TEXT;
