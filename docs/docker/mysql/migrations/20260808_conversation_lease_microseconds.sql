ALTER TABLE agent_conversation
    ADD COLUMN active_turn_claim_token CHAR(32) NULL,
    ADD COLUMN turn_abandoned_at DATETIME(6) NULL,
    MODIFY updated_at DATETIME(6) NOT NULL
        DEFAULT CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6);

ALTER TABLE agent_message
    ADD COLUMN semantic_fingerprint CHAR(64) NULL;
