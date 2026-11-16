# 002：补 parent_dir（父目录名）与可入库清单视图
-- 注：ADD COLUMN 不是幂等的，由 tools/kb/db.py 迁移函数先查 pragma 再执行；
-- 本文件仅作为"意图声明"与手工执行参考。

ALTER TABLE source_documents ADD COLUMN parent_dir TEXT;

CREATE INDEX IF NOT EXISTS idx_sd_parent ON source_documents(parent_dir);

-- 确认可入库的清单：分类完成、且不在隔离区（99- 下的记录永不入库）
DROP VIEW IF EXISTS ingest_queue;
CREATE VIEW ingest_queue AS
SELECT doc_hash, file_name, parent_dir, dir_path, ext, size_bytes,
       category, topic, sub_topic, collection, source_trust
FROM source_documents
WHERE status = 'classified'
  AND dir_path NOT LIKE '99-重复文件待删%';
