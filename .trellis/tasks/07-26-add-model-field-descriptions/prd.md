# 补齐模型字段描述

## Goal

统一 `src/data_agent` 中 Pydantic 领域模型的字段元数据，让每个模型属性都能在
生成的 JSON Schema 中提供非空中文 `description`，与现有配置模型保持一致。

## Requirements

- 覆盖 `src/data_agent/models/` 和 `src/data_agent/conversation/models.py` 中继承
  `ContractModel` 的模型字段。
- 为缺少描述的字段补充 `Field(description="...")`，不改变字段类型、默认值、校验约束
  或运行时序列化行为。
- 增加回归测试，递归检查目标模型的每个字段都有包含中文字符的非空描述。
- 保持现有代码风格，并通过相关 Python 质量检查。

## Acceptance Criteria

- [x] 目标 Pydantic 模型的所有字段都有非空中文 `Field.description`。
- [x] 回归测试能在缺少描述时失败，并在当前实现下通过。
- [x] 现有模型校验与测试不回归。

## Notes

- Keep `prd.md` focused on requirements, constraints, and acceptance criteria.
- Lightweight tasks can remain PRD-only.
- For complex tasks, add `design.md` for technical design and `implement.md` for execution planning before `task.py start`.
