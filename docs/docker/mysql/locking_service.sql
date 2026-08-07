-- 为 Query 共享读与 generation 写侧独占协调安装 MySQL 8.4 官方函数。
-- 脚本仅供空环境 bootstrap 或停服后的管理员显式执行，不是运行时 migration。
DROP FUNCTION IF EXISTS service_get_read_locks;
DROP FUNCTION IF EXISTS service_get_write_locks;
DROP FUNCTION IF EXISTS service_release_locks;

CREATE FUNCTION service_get_read_locks RETURNS INT
    SONAME 'locking_service.so';
CREATE FUNCTION service_get_write_locks RETURNS INT
    SONAME 'locking_service.so';
CREATE FUNCTION service_release_locks RETURNS INT
    SONAME 'locking_service.so';
