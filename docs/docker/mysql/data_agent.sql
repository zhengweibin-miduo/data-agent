SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS data_agent
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;
GRANT ALL PRIVILEGES ON data_agent.* TO 'data_agent'@'%';

USE data_agent;

CREATE TABLE IF NOT EXISTS llm_memory
(
    id                 BIGINT AUTO_INCREMENT PRIMARY KEY,
    uid                CHAR(64) NOT NULL UNIQUE,
    source             VARCHAR(128) NOT NULL,
    kind               VARCHAR(32) NOT NULL,
    scope_key          VARCHAR(256) NOT NULL,
    schema_fingerprint CHAR(64) NOT NULL,
    row_status         VARCHAR(16) NOT NULL DEFAULT 'NORMAL',
    pinned             BOOLEAN NOT NULL DEFAULT FALSE,
    content            JSON NOT NULL,
    payload            JSON NOT NULL,
    content_version    VARCHAR(32) NOT NULL,
    created_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at         DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                                       ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_llm_memory_lookup
        (source, kind, scope_key, schema_fingerprint, row_status)
) ENGINE = InnoDB;

CREATE TABLE IF NOT EXISTS llm_memory_relation
(
    memory_id         BIGINT NOT NULL,
    related_memory_id BIGINT NOT NULL,
    relation_type     VARCHAR(32) NOT NULL,
    PRIMARY KEY (memory_id, related_memory_id, relation_type),
    CONSTRAINT fk_llm_memory_relation_memory
        FOREIGN KEY (memory_id) REFERENCES llm_memory (id),
    CONSTRAINT fk_llm_memory_relation_related
        FOREIGN KEY (related_memory_id) REFERENCES llm_memory (id)
) ENGINE = InnoDB;
