-- 从 metadata semantic value index 功能合入前的 Meta schema 保数据升级。
-- 仅对尚未包含以下两列的既有环境执行一次；新环境继续使用 meta.sql 初始化。
USE meta;

ALTER TABLE table_info
    ADD COLUMN alias JSON NULL COMMENT '表别名' AFTER description;

ALTER TABLE column_info
    ADD COLUMN index_profile JSON NULL COMMENT '字段值索引资格事实' AFTER alias;

UPDATE column_info
SET index_profile = JSON_OBJECT(
    'decision', 'skip',
    'sensitivity', 'unknown',
    'evidence', JSON_ARRAY(id, table_id),
    'reason', '升级后等待重新生成字段值索引资格'
)
WHERE index_profile IS NULL;

ALTER TABLE column_info
    MODIFY COLUMN index_profile JSON NOT NULL COMMENT '字段值索引资格事实';
