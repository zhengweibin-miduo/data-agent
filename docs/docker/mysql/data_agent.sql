SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS data_agent
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;
GRANT ALL PRIVILEGES ON data_agent.* TO 'data_agent'@'%';

USE data_agent;

CREATE TABLE IF NOT EXISTS agent_memory
(
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '权威记忆的内部自增主键',
    uid                CHAR(64) NOT NULL UNIQUE COMMENT '基于记忆内容与作用域生成的稳定唯一标识',
    source             VARCHAR(128) NOT NULL COMMENT '记忆所属的稳定语义来源标识',
    user_id            VARCHAR(128) NULL COMMENT '对话记忆所属用户，DDL 记忆为空',
    kind               VARCHAR(32) NOT NULL COMMENT '记忆业务类型，如 DDL 事实或跨会话用户记忆',
    scope_key          VARCHAR(256) NOT NULL COMMENT '记忆对应表、字段或指标的稳定作用域标识',
    schema_fingerprint CHAR(64) NOT NULL COMMENT '生成记忆时物理结构快照的 SHA-256 指纹',
    memory_text        TEXT NOT NULL COMMENT '用于全文检索与向量检索的规范化记忆文本',
    content            JSON NOT NULL COMMENT '权威长期记忆的结构化业务内容',
    content_hash       CHAR(64) NOT NULL COMMENT '结构化内容的 SHA-256 哈希，用于幂等去重',
    trust              VARCHAR(32) NOT NULL COMMENT '事实可信来源，区分模型校验与用户确认',
    status             VARCHAR(16) NOT NULL DEFAULT 'ACTIVE' COMMENT '记忆生命周期状态，如 ACTIVE 或 DELETED',
    content_version    VARCHAR(32) NOT NULL COMMENT '结构化记忆内容的格式版本',
    projection_version VARCHAR(32) NOT NULL COMMENT 'Elasticsearch 与 Qdrant 索引投影的格式版本',
    created_job_id     CHAR(64) NULL COMMENT '首次生成该记忆的 DDL 任务标识，对话记忆为空',
    created_conversation_uid CHAR(64) NULL COMMENT '首次生成用户记忆的会话标识',
    created_message_uid CHAR(64) NULL COMMENT '首次生成用户记忆的证据消息标识',
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '权威记忆首次创建时间',
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP COMMENT '权威记忆最近更新时间',
    deleted_at         DATETIME NULL COMMENT '记忆被软删除的时间，未删除时为空',
    purge_requested_at DATETIME NULL COMMENT '用户级清除请求时间，普通软删除为空',
    INDEX idx_agent_memory_exact
        (source, kind, scope_key, schema_fingerprint, status),
    INDEX idx_agent_memory_rebuild (status, id),
    INDEX idx_agent_memory_user
        (user_id, kind, status, updated_at),
    UNIQUE KEY uq_agent_memory_content
        (source, kind, scope_key, schema_fingerprint, content_hash)
) ENGINE = InnoDB COMMENT = '存储经验证的权威长期记忆及其生命周期状态';

CREATE TABLE IF NOT EXISTS agent_conversation
(
    id                         BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '会话内部自增主键及稳定分页游标',
    uid                        CHAR(64) NOT NULL UNIQUE COMMENT '会话公开稳定标识',
    user_id                    VARCHAR(128) NOT NULL COMMENT '拥有会话的用户标识',
    summary                    TEXT NULL COMMENT '异步生成的有界会话摘要',
    summary_through_message_id BIGINT NULL COMMENT '摘要已经覆盖到的消息内部主键',
    active_turn_uid            CHAR(64) NULL COMMENT '当前唯一在途轮次标识',
    created_at                 DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '会话创建时间',
    updated_at                 DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                             ON UPDATE CURRENT_TIMESTAMP COMMENT '会话最近活动时间',
    INDEX idx_agent_conversation_user (user_id, updated_at, id)
) ENGINE = InnoDB COMMENT = '永久保存用户拥有的 Agent 文本会话';

CREATE TABLE IF NOT EXISTS agent_message
(
    id              BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '消息内部自增主键及稳定历史游标',
    uid             CHAR(64) NOT NULL UNIQUE COMMENT '消息公开稳定标识',
    user_id         VARCHAR(128) NOT NULL COMMENT '消息所属用户标识',
    conversation_id BIGINT NOT NULL COMMENT '消息所属会话内部主键',
    turn_uid        CHAR(64) NOT NULL COMMENT '消息所属幂等轮次标识',
    role            VARCHAR(16) NOT NULL COMMENT '纯文本消息角色，仅允许 user 或 assistant',
    content         MEDIUMTEXT NOT NULL COMMENT '永久保存的纯文本消息内容',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '消息创建时间',
    UNIQUE KEY uq_agent_message_turn_role
        (conversation_id, turn_uid, role),
    INDEX idx_agent_message_history (user_id, conversation_id, id),
    CONSTRAINT fk_agent_message_conversation
        FOREIGN KEY (conversation_id) REFERENCES agent_conversation (id)
            ON DELETE CASCADE,
    CONSTRAINT chk_agent_message_role CHECK (role IN ('user', 'assistant'))
) ENGINE = InnoDB COMMENT = '永久保存 Agent 会话的用户与助手纯文本消息';

CREATE TABLE IF NOT EXISTS conversation_memory_outbox
(
    id                   BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '提炼任务内部自增主键',
    user_id              VARCHAR(128) NOT NULL COMMENT '待提炼轮次所属用户标识',
    conversation_id      BIGINT NOT NULL COMMENT '待提炼轮次所属会话内部主键',
    turn_uid             CHAR(64) NOT NULL COMMENT '待提炼的幂等轮次标识',
    user_message_id      BIGINT NOT NULL COMMENT '轮次用户消息内部主键',
    assistant_message_id BIGINT NOT NULL COMMENT '轮次助手消息内部主键',
    attempts             INT NOT NULL DEFAULT 0 COMMENT '已经失败的提炼尝试次数',
    available_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '任务可领取或重试的最早时间',
    lease_token          CHAR(32) NULL COMMENT '当前 worker 的短期领取令牌',
    lease_expires_at     DATETIME NULL COMMENT '当前领取令牌的失效时间',
    last_error_type      VARCHAR(128) NULL COMMENT '最近一次失败的安全异常类型',
    created_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '提炼任务创建时间',
    updated_at           DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                            ON UPDATE CURRENT_TIMESTAMP COMMENT '提炼任务最近更新时间',
    UNIQUE KEY uq_conversation_memory_turn (conversation_id, turn_uid),
    INDEX idx_conversation_memory_claim
        (available_at, lease_expires_at, id),
    CONSTRAINT fk_conversation_memory_conversation
        FOREIGN KEY (conversation_id) REFERENCES agent_conversation (id)
            ON DELETE CASCADE
) ENGINE = InnoDB COMMENT = '异步提炼完成对话轮次长期记忆的可重试发件箱';

CREATE TABLE IF NOT EXISTS agent_memory_event
(
    id          BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '记忆历史事件的内部自增主键',
    memory_id   BIGINT NOT NULL COMMENT '事件所属权威记忆的内部主键',
    event_type  VARCHAR(16) NOT NULL COMMENT '历史事件类型，如新增、更新、删除或关联',
    old_content JSON NULL COMMENT '事件发生前的结构化记忆内容',
    new_content JSON NULL COMMENT '事件发生后的结构化记忆内容',
    job_id      CHAR(64) NULL COMMENT '触发事件的 DDL 任务标识，非任务事件可为空',
    actor_type  VARCHAR(16) NOT NULL COMMENT '事件执行者类型，区分工作流、用户与系统',
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '历史事件创建时间',
    INDEX idx_agent_memory_event_history (memory_id, id),
    CONSTRAINT fk_agent_memory_event_memory
        FOREIGN KEY (memory_id) REFERENCES agent_memory (id)
) ENGINE = InnoDB COMMENT = '以只追加方式记录权威记忆的新增、更新、删除与关联历史';

CREATE TABLE IF NOT EXISTS agent_memory_link
(
    memory_id        BIGINT NOT NULL COMMENT '关联关系起点记忆的内部主键',
    linked_memory_id BIGINT NOT NULL COMMENT '关联关系终点记忆的内部主键',
    link_type        VARCHAR(32) NOT NULL COMMENT '记忆关联类型，如相关、派生来源或替代',
    PRIMARY KEY (memory_id, linked_memory_id, link_type),
    CONSTRAINT fk_agent_memory_link_memory
        FOREIGN KEY (memory_id) REFERENCES agent_memory (id),
    CONSTRAINT fk_agent_memory_link_linked
        FOREIGN KEY (linked_memory_id) REFERENCES agent_memory (id)
) ENGINE = InnoDB COMMENT = '维护权威记忆之间有方向的业务关联关系';

CREATE TABLE IF NOT EXISTS memory_index_outbox
(
    memory_uid         CHAR(64) NOT NULL COMMENT '待同步权威记忆的稳定唯一标识',
    target             VARCHAR(16) NOT NULL COMMENT '派生索引目标，如 Elasticsearch 或 Qdrant',
    operation          VARCHAR(16) NOT NULL COMMENT '索引期望操作，如 UPSERT 或 DELETE',
    projection_version VARCHAR(32) NOT NULL COMMENT '本次同步使用的索引投影格式版本',
    attempts           INT NOT NULL DEFAULT 0 COMMENT '已经失败的投递尝试次数',
    available_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '记录可被工作进程领取或重试的最早时间',
    last_error_type    VARCHAR(128) NULL COMMENT '最近一次投递失败的错误类型',
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                         ON UPDATE CURRENT_TIMESTAMP COMMENT '索引期望状态最近更新时间',
    PRIMARY KEY (memory_uid, target),
    INDEX idx_memory_index_outbox_claim (available_at, updated_at),
    CONSTRAINT fk_memory_index_outbox_memory
        FOREIGN KEY (memory_uid) REFERENCES agent_memory (uid)
) ENGINE = InnoDB COMMENT = '记录同步权威记忆到派生索引的可重试期望状态';
