# 初始化 MySQL 示例数据库

## Goal

让 `docs/docker/docker-compose.yml` 中的 MySQL 容器在首次初始化数据卷时，自动执行 `docs/docker/mysql/` 下的 SQL，并确保应用用户能够访问新建数据库。

## Background

- MySQL 使用官方 `mysql:8.4` 镜像和持久卷 `mysql_data`。
- `docs/docker/mysql/dw.sql` 与 `meta.sql` 分别创建 `dw`、`meta` 数据库及示例表。
- Compose 创建的业务用户是 `data_agent`，现有 SQL 却向不存在的 `atguigu` 用户授权。
- 官方镜像只会在 `/var/lib/mysql` 为空时执行 `/docker-entrypoint-initdb.d/` 中的初始化脚本。

## Requirements

- 将本地 `./mysql` 目录只读挂载到 MySQL 容器的 `/docker-entrypoint-initdb.d`。
- 将 `dw.sql` 和 `meta.sql` 的授权用户由 `atguigu` 改为 Compose 已创建的 `data_agent`。
- 保留现有 `mysql_data` 持久卷及其他服务配置。
- 不实现“每次容器重启都重新执行 SQL”；已有数据卷不得被配置变更自动清空或重建。

## Acceptance Criteria

- [ ] `docker compose config` 能解析配置，并显示 MySQL 初始化目录的只读挂载。
- [ ] 空 MySQL 数据卷首次启动时会按文件名顺序执行 `dw.sql`、`meta.sql`。
- [ ] 两个 SQL 均向 `'data_agent'@'%'` 授予对应数据库权限，不再引用 `atguigu`。
- [ ] 现有 MySQL 数据卷在普通容器重启时保持不变，初始化脚本不会重复执行。

## Out of Scope

- 自动删除已有数据卷。
- 为每次启动强制执行初始化 SQL 的自定义入口脚本。
- 修改 SQL 中既有的建表或示例数据内容。
