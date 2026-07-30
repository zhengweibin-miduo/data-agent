SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS data_sync
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_bin;
GRANT ALL PRIVILEGES ON data_sync.* TO 'data_agent'@'%';

USE data_sync;

CREATE TABLE IF NOT EXISTS data_sync_task
(
    id                    BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '同步任务内部自增主键',
    source                VARCHAR(128) NOT NULL COMMENT '服务端命名数据源配置键',
    source_schema         VARCHAR(64) NOT NULL COMMENT '源 MySQL 数据库名称',
    source_table          VARCHAR(64) NOT NULL COMMENT '源业务表名称',
    target_table          VARCHAR(64) NOT NULL COMMENT '不带来源前缀的统一 DW 表名称',
    desired_json          JSON NOT NULL COMMENT 'Meta 快照派生的有界同步期望状态',
    desired_hash          CHAR(64) NOT NULL COMMENT '同步期望状态的 SHA-256 哈希',
    phase                 VARCHAR(32) NOT NULL DEFAULT 'pending_schema' COMMENT '结构、回填、回放或实时同步阶段',
    snapshot_file         VARCHAR(255) NULL COMMENT '首次回填基线对应的 Binlog 文件',
    snapshot_position     BIGINT NULL COMMENT '首次回填基线对应的 Binlog 位置',
    captured_file         VARCHAR(255) NULL COMMENT '已持久化到事件缓冲区的 Binlog 文件',
    captured_position     BIGINT NULL COMMENT '已持久化到事件缓冲区的 Binlog 位置',
    captured_row_index    INT NULL COMMENT '同一 Binlog 事件内已捕获的行序号',
    applied_file          VARCHAR(255) NULL COMMENT '已成功应用到 DW 的 Binlog 文件',
    applied_position      BIGINT NULL COMMENT '已成功应用到 DW 的 Binlog 位置',
    applied_row_index     INT NULL COMMENT '同一 Binlog 事件内已应用的行序号',
    last_backfill_key     JSON NULL COMMENT '最后完成回填批次的有序主键值',
    attempts              INT NOT NULL DEFAULT 0 COMMENT '已消费的可重试失败次数',
    available_at          DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '下一次允许领取任务的数据库时间',
    lease_token           CHAR(32) NULL COMMENT '当前 worker 持有的短期租约令牌',
    lease_expires_at      DATETIME NULL COMMENT '当前 worker 租约到期的数据库时间',
    worker_heartbeat_at   DATETIME NULL COMMENT '仅由 CDC worker 更新的最近活性时间',
    last_error_type       VARCHAR(128) NULL COMMENT '最近一次失败的安全异常类型',
    created_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '同步任务首次创建时间',
    updated_at            DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                          ON UPDATE CURRENT_TIMESTAMP COMMENT '同步任务最近更新时间',
    UNIQUE KEY uq_data_sync_task_identity
        (source, source_schema, source_table, target_table),
    UNIQUE KEY uq_data_sync_task_source_target (source, target_table),
    INDEX idx_data_sync_task_claim (phase, available_at, lease_expires_at)
) ENGINE = InnoDB COMMENT = '保存每张源表到统一 DW 表的当前同步期望与恢复进度';

CREATE TABLE IF NOT EXISTS data_sync_event
(
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '暂存事件内部自增主键',
    task_id            BIGINT NOT NULL COMMENT '事件所属同步任务内部主键',
    source             VARCHAR(128) NOT NULL COMMENT '事件所属命名数据源配置键',
    binlog_file        VARCHAR(255) NOT NULL COMMENT '事件来源 Binlog 文件',
    binlog_position    BIGINT NOT NULL COMMENT '事件来源 Binlog 位置',
    row_index          INT NOT NULL COMMENT '同一 Binlog 事件内的行序号',
    payload_json       JSON NOT NULL COMMENT '规范化且可逆的单行变更载荷',
    acknowledged_at    DATETIME NULL COMMENT '事件已成功应用到 DW 的确认时间',
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '暂存事件创建时间',
    UNIQUE KEY uq_data_sync_event_coordinate
        (source, binlog_file, binlog_position, row_index),
    INDEX idx_data_sync_event_replay (task_id, acknowledged_at, id)
) ENGINE = InnoDB COMMENT = '回填和追平期间暂存可幂等回放的 Binlog 行事件';

CREATE TABLE IF NOT EXISTS data_sync_key_owner
(
    target_table       VARCHAR(64) NOT NULL COMMENT '统一 DW 目标表名称',
    primary_key_hash   CHAR(64) NOT NULL COMMENT '规范化目标主键文档的 SHA-256 哈希',
    primary_key_json   TEXT NOT NULL COMMENT '用于复核哈希碰撞的完整规范化主键文档',
    source             VARCHAR(128) NOT NULL COMMENT '首次成功写入该目标主键的数据源',
    deleted            BOOLEAN NOT NULL DEFAULT FALSE COMMENT '源行已删除但归属仍保留的墓碑标记',
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '目标主键首次建立归属的时间',
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                           ON UPDATE CURRENT_TIMESTAMP COMMENT '目标主键归属最近更新时间',
    PRIMARY KEY (target_table, primary_key_hash),
    INDEX idx_data_sync_key_owner_source (source, target_table)
) ENGINE = InnoDB COMMENT = '保存统一 DW 目标主键的首次来源归属及删除墓碑';

CREATE TABLE IF NOT EXISTS metadata_index_outbox
(
    target             VARCHAR(16) NOT NULL COMMENT 'semantic 或 values 索引目标',
    object_kind        VARCHAR(16) NOT NULL COMMENT 'table、column 或 metric 对象类型',
    object_id          VARCHAR(128) NOT NULL COMMENT 'Meta 对象或值刷新表标识',
    operation          VARCHAR(16) NOT NULL COMMENT 'upsert、delete 或 refresh 期望操作',
    desired_version    CHAR(64) NOT NULL COMMENT '合并期望状态版本',
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
