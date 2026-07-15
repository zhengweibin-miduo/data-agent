# 接入 Loguru 日志

## Goal

为应用提供统一、可配置的控制台和文件日志入口，复用现有 YAML 日志配置，并让异步调用能够通过 `trace_id` 关联日志。

## Background

- `pyproject.toml` 尚未包含日志依赖。
- `conf/app.yaml` 已定义控制台和文件开关、级别、目录、`rotation: "10 MB"` 与 `retention: "7 days"`。
- `app/conf/app_config.py` 已使用 Pydantic 校验上述配置。
- 当前入口 `main.py` 只有临时 `print`，四个客户端管理器尚未记录生命周期日志。

## Requirements

- 使用 Loguru，不同时引入其他日志框架。
- 日志初始化、格式常量和 sink 配置统一放在 `app/core` 下；其他模块不得各自重复配置 sink。
- 提供单一日志初始化入口，并从 `app_config.logging` 读取控制台及文件配置。
- 控制台日志使用带颜色的单行格式：毫秒时间、级别、源码位置、`trace_id`、消息。
- 文件日志使用无颜色的单行格式：毫秒时间、级别、进程 ID、源码位置、`trace_id`、消息。
- 未绑定链路信息时，`trace_id` 输出 `-`，不得因缺少 extra 字段导致日志失败。
- 文件日志写入 `logs/data-agent.log`，轮转大小与保留时长直接复用现有配置。
- 初始化时移除 Loguru 默认 sink，确保重复初始化不会重复输出。
- 日志不得输出密码、API Key、完整数据库连接串等敏感配置。

## Acceptance Criteria

- [ ] `loguru` 已加入项目依赖并更新锁文件。
- [ ] 日志配置实现位于 `app/core`，业务模块只复用统一入口或已配置的 `logger`。
- [ ] 控制台和文件 sink 可分别通过现有配置启停及设置最低级别。
- [ ] 日志行符合已确认格式，未绑定 `trace_id` 时显示 `trace_id=-`。
- [ ] 文件目录不存在时可自动创建，轮转与保留配置生效。
- [ ] 重复调用初始化不会造成同一条日志重复输出。
- [ ] 留下一个覆盖初始化关键行为的最小可运行检查。

## Out of Scope

- JSON 日志、OpenTelemetry、ELK 专用字段及标准库日志拦截。
- 为每个业务调用或客户端生命周期批量补日志，除非用户明确纳入本次范围。
- 自定义日志封装类或额外抽象层。

## Notes

- 这是轻量任务，PRD-only 即可。
