-- 保留既有会话数据的 PR #85 兼容升级；仅对父提交 schema 执行一次。
ALTER TABLE agent_message
    ADD COLUMN semantic_fingerprint CHAR(64) NULL
        COMMENT '决定轮次语义或 Query 终态的内部指纹'
        AFTER content;
