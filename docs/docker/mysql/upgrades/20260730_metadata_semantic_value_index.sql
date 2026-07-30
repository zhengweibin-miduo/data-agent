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

CREATE DATABASE IF NOT EXISTS data_sync CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
USE data_sync;

CREATE TABLE IF NOT EXISTS metadata_index_outbox
(
    target             VARCHAR(16) NOT NULL COMMENT 'semantic 或 values 索引目标',
    object_kind        VARCHAR(16) NOT NULL COMMENT 'table、column 或 metric 对象类型',
    object_id          VARCHAR(128) NOT NULL COMMENT 'Meta 对象或值刷新表标识',
    operation          VARCHAR(16) NOT NULL COMMENT 'upsert、delete 或 refresh 期望操作',
    desired_version    CHAR(64) NOT NULL COMMENT '合并期望状态版本',
    pending_desired_version CHAR(64) NULL COMMENT '当前刷新完成后待处理的最新版本',
    progress_column_id VARCHAR(128) NULL COMMENT 'values 刷新最后完成的字段标识',
    attempts           INT NOT NULL DEFAULT 0 COMMENT '远程失败次数',
    available_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '下次允许领取时间',
    lease_token        CHAR(32) NULL COMMENT '当前领取令牌',
    lease_expires_at   DATETIME NULL COMMENT '当前领取到期时间',
    last_error_type    VARCHAR(128) NULL COMMENT '最近安全异常类型',
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '期望状态创建时间',
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                      ON UPDATE CURRENT_TIMESTAMP COMMENT '期望状态更新时间',
    PRIMARY KEY (target, object_kind, object_id),
    INDEX idx_metadata_index_outbox_claim (available_at, lease_expires_at, attempts)
) ENGINE = InnoDB COMMENT = '保存 Meta 语义与字段值派生索引的可合并期望状态';
