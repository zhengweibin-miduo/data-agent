# Data Agent 前端能力证据

## 产品边界

- `README.md:1-3`：产品明确面向 MySQL DDL，使用确定性解析、结构化 LLM、LangGraph 可恢复工作流、人工澄清和长期记忆生成语义元数据与指标。
- `README.md:20-31`：本地依赖包含 MySQL、Redis、Qdrant、Elasticsearch、TEI，宿主端口只绑定 `127.0.0.1`。
- `.trellis/spec/frontend/component-guidelines.md:3-19` 与 `.trellis/spec/frontend/directory-structure.md:3-25`：当前没有前端框架、组件、JS/TS/HTML/CSS 或构建配置。

## 可直接映射到界面的能力

### DDL 任务闭环

- `src/data_agent/ddl_metadata/api/jobs.py:24-45`：提交任务返回 `202`、`job_id`、`status_url`、`events_url`；注释明确 202 不代表执行开始或完成。
- `src/data_agent/ddl_metadata/api/jobs.py:48-82`：可查询公开任务投影并订阅只读、可重连 SSE；内部 LangGraph 节点载荷不能显示给用户。
- `src/data_agent/ddl_metadata/api/jobs.py:85-102`：可按当前修订提交澄清回答。
- `src/data_agent/models/jobs.py:13-54`：公开状态与阶段可直接形成状态机和进度文案。
- `src/data_agent/models/jobs.py:67-93`：成功结果只有 DDL 哈希及表、列、指标数量；不能设计不存在的完整元数据结果页。
- `src/data_agent/models/jobs.py:122-150`：输入和回答的真实校验边界。
- `src/data_agent/models/semantic.py:67-89`：澄清问题包含文本、事实表、关联列和必填标识，回答是单题自由文本。

### 知识记忆

- `src/data_agent/ddl_metadata/api/memories.py:23-97`：支持按 `source + query` 搜索、读取详情/历史、带版本更新与软删除。
- 更新记忆会要求重新处理 DDL，因此修正动作必须提示影响，不能包装成无副作用的普通文本编辑。

## 现阶段不能承诺的能力

- `.trellis/spec/backend/conversation-memory.md:5-13`：第一版是单 Agent 文本消息；认证、Agent 注册、附件和多模态不在范围内。
- `src/data_agent/conversation/api.py:41-130`：会话 API 负责消息与上下文持久化，但没有调用 Agent/LLM 的端点；不能设计可工作的 AI 聊天主流程。
- 全部路由没有任务列表接口；不能设计服务端历史任务页。
- 任务成功结果没有表、列、指标明细接口；不能设计完整语义目录。
- `src/data_agent/answer_readiness/service.py:14-15,43-60`：数据未就绪或意图不明时只能显示安全用户文案，内部诊断 reason 不应进入 UI。

## MVP 推论

最小完整产品是一个本地 DDL 语义化工作台：提交 DDL、跟随公开处理轨迹、回答澄清、看到结果摘要，再按来源检索和修正可复用知识。通用聊天、运营指标卡和复杂管理后台既不是核心问题，也没有后端契约支撑。
