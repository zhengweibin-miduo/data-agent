"""Schema Loom 静态前端入口检查。"""

from httpx import ASGITransport, AsyncClient

from data_agent.application import create_app
from tests.helpers.checks import check_condition, check_equal


async def test_frontend_routes_and_assets_are_served_without_lifespan() -> None:
    """工作台、知识页和静态资源由同一个本地 FastAPI 应用提供。"""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        workbench = await client.get("/workbench")
        restored = await client.get("/workbench/job-1")
        knowledge = await client.get("/knowledge")
        script = await client.get("/assets/app.js")
        styles = await client.get("/assets/styles.css")

    for name, response in {
        "工作台": workbench,
        "任务恢复": restored,
        "知识记忆": knowledge,
        "脚本": script,
        "样式": styles,
    }.items():
        check_equal(f"{name} HTTP 状态", response.status_code, 200)
    check_condition(
        "工作台包含 Schema Trace",
        "Schema Trace" in workbench.text,
        actual=workbench.text[:200],
        expected="Schema Trace",
    )
    check_condition(
        "浏览器只调用服务端聊天端点",
        "/chat-turns" in script.text and "DATA_AGENT_LLM_API_KEY" not in script.text,
        actual="chat-turns" if "/chat-turns" in script.text else "missing",
        expected="服务端 chat-turns 且无模型密钥",
    )
    check_condition(
        "SSE 等待澄清先回读权威任务",
        'data.status === "waiting_input"' in script.text
        and "renderJob(await refreshJob()" in script.text
        and "question_set_id: data.status" in script.text,
        actual="waiting_input 权威回读标记",
        expected="GET JobRecord 后再呈现可提交问题",
    )
    check_condition(
        "聊天失败重试复用原轮次",
        "state.failedChat = attempt" in script.text
        and "sendChat(state.failedChat, false)" in script.text,
        actual="聊天重试轮次标记",
        expected="复用失败请求的 turn_uid",
    )
    check_condition(
        "画布调用只读 preview 契约",
        "/api/v1/metadata/ddl-preview" in script.text
        and 'id="schema-nodes"' in workbench.text
        and 'id="relationship-layer"' in workbench.text,
        actual="preview 与 lineage DOM 标记",
        expected="真实 parser preview 驱动画布",
    )
    check_condition(
        "工作台无宣传 hero 并保持三栏结构",
        "view-heading" not in workbench.text
        and "workbench-grid" in workbench.text
        and "LIVE LINEAGE" in workbench.text,
        actual="Semantic Night Canvas 外壳",
        expected="无 hero 的紧凑三栏画布",
    )
    check_condition(
        "样式包含六个 Semantic Night token",
        all(
            token in styles.text
            for token in (
                "--canvas-ink",
                "--node-slate",
                "--data-cyan",
                "--semantic-violet",
                "--metric-amber",
                "--ice-text",
            )
        ),
        actual="CSS token 集",
        expected="六个具名 token",
    )
