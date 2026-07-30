SET NAMES utf8mb4;

CREATE DATABASE IF NOT EXISTS source_demo
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_general_ci;

CREATE USER IF NOT EXISTS 'data_agent_replica'@'%'
    IDENTIFIED BY 'data_agent_replica';
GRANT SELECT ON source_demo.* TO 'data_agent_replica'@'%';
GRANT REPLICATION SLAVE, REPLICATION CLIENT ON *.*
    TO 'data_agent_replica'@'%';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, DROP
    ON source_demo.* TO 'data_agent'@'%';
