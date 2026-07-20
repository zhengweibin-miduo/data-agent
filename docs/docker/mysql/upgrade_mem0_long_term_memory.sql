SET NAMES utf8mb4;

USE data_agent;

ALTER TABLE agent_memory
    ADD COLUMN user_id VARCHAR(128) NULL
        COMMENT '对话记忆所属用户，DDL 记忆为空' AFTER source,
    MODIFY COLUMN created_job_id CHAR(64) NULL
        COMMENT '首次生成该记忆的 DDL 任务标识，对话记忆为空',
    ADD COLUMN created_conversation_uid CHAR(64) NULL
        COMMENT '首次生成用户记忆的会话标识' AFTER created_job_id,
    ADD COLUMN created_message_uid CHAR(64) NULL
        COMMENT '首次生成用户记忆的证据消息标识' AFTER created_conversation_uid,
    ADD COLUMN purge_requested_at DATETIME NULL
        COMMENT '用户级清除请求时间，普通软删除为空' AFTER deleted_at,
    ADD INDEX idx_agent_memory_user (user_id, kind, status, updated_at);

UPDATE agent_memory
SET projection_version = 'v2'
WHERE projection_version <> 'v2';

CREATE TABLE agent_conversation
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

CREATE TABLE agent_message
(
    id              BIGINT AUTO_INCREMENT PRIMARY KEY COMMENT '消息内部自增主键及稳定历史游标',
    uid             CHAR(64) NOT NULL UNIQUE COMMENT '消息公开稳定标识',
    user_id         VARCHAR(128) NOT NULL COMMENT '消息所属用户标识',
    conversation_id BIGINT NOT NULL COMMENT '消息所属会话内部主键',
    turn_uid        CHAR(64) NOT NULL COMMENT '消息所属幂等轮次标识',
    role            VARCHAR(16) NOT NULL COMMENT '纯文本消息角色，仅允许 user 或 assistant',
    content         TEXT NOT NULL COMMENT '永久保存的纯文本消息内容',
    created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '消息创建时间',
    UNIQUE KEY uq_agent_message_turn_role
        (conversation_id, turn_uid, role),
    INDEX idx_agent_message_history (user_id, conversation_id, id),
    CONSTRAINT fk_agent_message_conversation
        FOREIGN KEY (conversation_id) REFERENCES agent_conversation (id)
            ON DELETE CASCADE,
    CONSTRAINT chk_agent_message_role CHECK (role IN ('user', 'assistant'))
) ENGINE = InnoDB COMMENT = '永久保存 Agent 会话的用户与助手纯文本消息';

CREATE TABLE conversation_memory_outbox
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
