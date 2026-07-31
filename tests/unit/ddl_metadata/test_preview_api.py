"""只读 DDL preview 契约检查。"""

from httpx import ASGITransport, AsyncClient

from data_agent.application import create_app
from tests.helpers.checks import check_condition, check_equal

DDL = """
CREATE TABLE customers (
    id BIGINT PRIMARY KEY
);
CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    CONSTRAINT fk_customer FOREIGN KEY (customer_id) REFERENCES customers(id)
);
"""


async def test_preview_returns_real_schema_without_application_lifespan() -> None:
    """Preview 直接解析真实表列外键，且不依赖任务或外部资源。"""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/metadata/ddl-preview",
            json={"source": "commerce", "dialect": "mysql", "ddl": DDL},
        )

    check_equal("preview 状态", response.status_code, 200)
    payload = response.json()
    check_equal("preview 表数", payload["table_count"], 2)
    check_equal("preview 列数", payload["column_count"], 3)
    check_equal("preview 外键数", len(payload["relationships"]), 1)
    relationship = payload["relationships"][0]
    check_equal(
        "preview 外键来源字段",
        relationship["source_column_id"],
        payload["tables"][1]["columns"][1]["id"],
    )
    check_equal(
        "preview 外键目标字段",
        relationship["target_column_id"],
        payload["tables"][0]["columns"][0]["id"],
    )


async def test_preview_reuses_safe_parser_error_projection() -> None:
    """无效 DDL 沿用稳定 parser 错误，不创建部分 preview。"""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/metadata/ddl-preview",
            json={"source": "commerce", "dialect": "mysql", "ddl": "DROP TABLE x"},
        )

    check_equal("preview 拒绝状态", response.status_code, 422)
    check_equal(
        "preview 拒绝错误",
        response.json()["error"]["code"],
        "unsupported_statement",
    )


async def test_preview_preserves_all_real_foreign_key_coordinates() -> None:
    """列级、复合、限定和外部外键均保留真实坐标且不误连同名表。"""
    ddl = """
    CREATE TABLE app.parent (
        id BIGINT,
        version INT,
        PRIMARY KEY (id, version)
    );
    CREATE TABLE app.child (
        id BIGINT PRIMARY KEY,
        inline_parent BIGINT REFERENCES app.parent(id),
        parent_id BIGINT,
        parent_version INT,
        external_id BIGINT,
        FOREIGN KEY (parent_id, parent_version) REFERENCES app.parent(id, version),
        FOREIGN KEY (external_id) REFERENCES ext.parent(id)
    );
    """
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/metadata/ddl-preview",
            json={"source": "commerce", "dialect": "mysql", "ddl": ddl},
        )

    check_equal("完整外键 preview 状态", response.status_code, 200)
    payload = response.json()
    relationships = payload["relationships"]
    check_equal("列级加复合加外部外键字段数", len(relationships), 4)
    check_equal(
        "外键目标顺序",
        [
            (item["target_table_name"], item["target_column_name"])
            for item in relationships
        ],
        [
            ("app.parent", "id"),
            ("app.parent", "id"),
            ("app.parent", "version"),
            ("ext.parent", "id"),
        ],
    )
    app_parent = payload["tables"][0]
    external = relationships[-1]
    check_condition(
        "外部限定表不误连本地同名节点",
        external["target_table_id"] != app_parent["id"],
        actual=external["target_table_id"],
        expected="不同于 app.parent 的稳定 ID",
    )


async def test_preview_request_contract_is_strict() -> None:
    """Preview 拒绝未知字段和非 MySQL 方言。"""
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unknown = await client.post(
            "/api/v1/metadata/ddl-preview",
            json={
                "source": "commerce",
                "dialect": "mysql",
                "ddl": "CREATE TABLE x (id BIGINT PRIMARY KEY)",
                "persist": True,
            },
        )
        dialect = await client.post(
            "/api/v1/metadata/ddl-preview",
            json={
                "source": "commerce",
                "dialect": "postgres",
                "ddl": "CREATE TABLE x (id BIGINT PRIMARY KEY)",
            },
        )

    check_equal("preview 未知字段拒绝", unknown.status_code, 422)
    check_equal("preview 非 MySQL 方言拒绝", dialect.status_code, 422)
