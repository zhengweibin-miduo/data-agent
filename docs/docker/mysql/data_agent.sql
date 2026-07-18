SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS data_agent
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;
GRANT ALL PRIVILEGES ON data_agent.* TO 'data_agent'@'%';

USE data_agent;

-- 尚未投入使用的旧 Memos 风格契约不迁移。
DROP TABLE IF EXISTS llm_memory_relation;
DROP TABLE IF EXISTS llm_memory;

CREATE TABLE IF NOT EXISTS agent_memory
(
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
    uid                CHAR(64) NOT NULL UNIQUE,
    source             VARCHAR(128) NOT NULL,
    kind               VARCHAR(32) NOT NULL,
    scope_key          VARCHAR(256) NOT NULL,
    schema_fingerprint CHAR(64) NOT NULL,
    memory_text        TEXT NOT NULL,
    content            JSON NOT NULL,
    content_hash       CHAR(64) NOT NULL,
    trust              VARCHAR(32) NOT NULL,
    status             VARCHAR(16) NOT NULL DEFAULT 'ACTIVE',
    content_version    VARCHAR(32) NOT NULL,
    projection_version VARCHAR(32) NOT NULL,
    created_job_id     CHAR(64) NOT NULL,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP,
    deleted_at         DATETIME NULL,
    INDEX idx_agent_memory_exact
        (source, kind, scope_key, schema_fingerprint, status),
    INDEX idx_agent_memory_rebuild (status, id),
    UNIQUE KEY uq_agent_memory_content
        (source, kind, scope_key, schema_fingerprint, content_hash)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS agent_memory_event
(
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    memory_id   BIGINT NOT NULL,
    event_type  VARCHAR(16) NOT NULL,
    old_content JSON NULL,
    new_content JSON NULL,
    job_id      CHAR(64) NULL,
    actor_type  VARCHAR(16) NOT NULL,
    created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_agent_memory_event_history (memory_id, id),
    CONSTRAINT fk_agent_memory_event_memory
        FOREIGN KEY (memory_id) REFERENCES agent_memory (id)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS agent_memory_link
(
    memory_id        BIGINT NOT NULL,
    linked_memory_id BIGINT NOT NULL,
    link_type        VARCHAR(32) NOT NULL,
    PRIMARY KEY (memory_id, linked_memory_id, link_type),
    CONSTRAINT fk_agent_memory_link_memory
        FOREIGN KEY (memory_id) REFERENCES agent_memory (id),
    CONSTRAINT fk_agent_memory_link_linked
        FOREIGN KEY (linked_memory_id) REFERENCES agent_memory (id)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS memory_index_outbox
(
    memory_uid         CHAR(64) NOT NULL,
    target             VARCHAR(16) NOT NULL,
    operation          VARCHAR(16) NOT NULL,
    projection_version VARCHAR(32) NOT NULL,
    attempts           INT NOT NULL DEFAULT 0,
    available_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_error_type    VARCHAR(128) NULL,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                         ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (memory_uid, target),
    INDEX idx_memory_index_outbox_claim (available_at, updated_at),
    CONSTRAINT fk_memory_index_outbox_memory
        FOREIGN KEY (memory_uid) REFERENCES agent_memory (uid)
) ENGINE = InnoDB;
