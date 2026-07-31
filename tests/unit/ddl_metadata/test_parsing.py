"""MySQL DDL 解析器检查。"""

import asyncio
import inspect
import threading
from typing import Any

import sqlglot
from pytest import MonkeyPatch
from sqlglot import expressions as exp
from sqlglot.errors import ParseError

from data_agent.ddl_metadata import parsing
from data_agent.ddl_metadata.parsing import parse_ddl
from data_agent.errors import DataAgentError
from data_agent.identifiers import scope_fingerprint
from data_agent.settings import app_config
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)

DDL = """
CREATE TABLE dim_customer (
    customer_id BIGINT COMMENT 'customer key',
    name VARCHAR(100),
    PRIMARY KEY (customer_id)
) COMMENT='customers';
CREATE TABLE sales.fact_order (
    order_id BIGINT PRIMARY KEY,
    customer_id BIGINT,
    amount DECIMAL(10, 2),
    CONSTRAINT fk_customer FOREIGN KEY (customer_id)
        REFERENCES dim_customer(customer_id)
) COMMENT='orders';
"""


async def _assert_rejected(ddl: str, code: str) -> None:
    """断言 DDL 被指定错误拒绝。"""
    try:
        await parse_ddl("test_source", ddl)
    except DataAgentError as error:
        check_exception("_assert_rejected 捕获预期异常", error, DataAgentError)
        check_equal("_assert_rejected 检查点 1", error.code, code)
        if code == "malformed_ddl":
            check_condition(
                "_assert_rejected 保留 SQLGlot 异常链",
                isinstance(error.__cause__, ParseError),
                actual=type(error.__cause__).__name__,
                expected="ParseError",
            )
    else:
        fail_check(
            "_assert_rejected",
            actual="未抛出预期异常",
            expected=f"DDL 应被 {code} 拒绝",
        )


async def test_ddl_parser() -> None:
    """覆盖多表、约束、注释、稳定 ID 和拒绝边界。"""
    check_condition(
        "公开解析器仅提供异步契约",
        inspect.iscoroutinefunction(parse_ddl),
        expected="async def parse_ddl",
    )
    check_condition(
        "不存在公开同步兼容别名",
        not hasattr(parsing, "parse_ddl_sync"),
        expected="仅保留私有 _parse_ddl_sync",
    )
    schema = await parse_ddl("test_source", DDL)
    repeated = await parse_ddl("test_source", DDL)
    check_equal("test_ddl_parser 检查点 1", schema, repeated)
    check_equal("test_ddl_parser 检查点 2", len(schema.tables), 2)
    check_equal(
        "test_ddl_parser 检查点 3",
        schema.tables[0].comment,
        "customers",
    )
    check_equal(
        "test_ddl_parser 检查点 4",
        schema.tables[0].columns[0].comment,
        "customer key",
    )
    check_equal(
        "test_ddl_parser 检查点 5",
        schema.tables[0].columns[0].structural_role,
        "primary_key",
    )
    check_equal(
        "test_ddl_parser 检查点 6",
        schema.tables[1].qualified_name,
        "sales.fact_order",
    )
    roles = {column.name: column.structural_role for column in schema.tables[1].columns}
    check_equal(
        "test_ddl_parser 检查点 7",
        roles,
        {
            "order_id": "primary_key",
            "customer_id": "foreign_key",
            "amount": None,
        },
    )
    check_equal("test_ddl_parser 检查点 8", len(schema.ddl_hash), 64)
    check_equal(
        "test_ddl_parser 检查点 9",
        len(schema.schema_fingerprint),
        64,
    )
    check_equal(
        "外键引用边包含真实目标表列",
        [
            (
                relationship.source_column_id,
                relationship.target_table,
                relationship.target_column,
            )
            for relationship in schema.relationships
        ],
        [(schema.tables[1].columns[1].id, "dim_customer", "customer_id")],
    )

    inline_reference = await parse_ddl(
        "test_source",
        "CREATE TABLE child (id BIGINT PRIMARY KEY, parent_id BIGINT "
        "REFERENCES parent(id))",
    )
    check_equal(
        "列级外键同样生成引用边",
        [
            (relationship.target_table, relationship.target_column)
            for relationship in inline_reference.relationships
        ],
        [("parent", "id")],
    )

    quoted_references = await parse_ddl(
        "test_source",
        "CREATE TABLE `sales`.`parent` (`ID` BIGINT PRIMARY KEY); "
        "CREATE TABLE `sales`.`table_child` ("
        "child_id BIGINT PRIMARY KEY, "
        "parent_id BIGINT REFERENCES `sales`.`parent`(`ID`)); "
        "CREATE TABLE `sales`.`constraint_child` ("
        "child_id BIGINT PRIMARY KEY, parent_id BIGINT, "
        "FOREIGN KEY (parent_id) REFERENCES `sales`.`parent`(`ID`))",
    )
    check_equal(
        "带反引号的列级和表级外键目标使用规范身份",
        [
            (relationship.target_table, relationship.target_column)
            for relationship in quoted_references.relationships
        ],
        [("sales.parent", "ID"), ("sales.parent", "ID")],
    )

    changed_reference = await parse_ddl(
        "test_source",
        DDL.replace(
            "REFERENCES dim_customer(customer_id)",
            "REFERENCES pii_customer(id)",
        ),
    )
    check_condition(
        "仅改变外键目标会更新全局指纹",
        changed_reference.schema_fingerprint != schema.schema_fingerprint,
        expected="关系边参与 schema_fingerprint",
    )
    source_column_id = schema.tables[1].columns[1].id
    check_condition(
        "仅改变外键目标会更新字段作用域指纹",
        scope_fingerprint(changed_reference, source_column_id)
        != scope_fingerprint(schema, source_column_id),
        expected="关系边参与字段作用域指纹",
    )

    reordered = await parse_ddl(
        "test_source",
        "CREATE TABLE composite (a BIGINT, b BIGINT, PRIMARY KEY (b, a))",
    )
    check_equal(
        "复合主键保留声明顺序",
        reordered.tables[0].primary_key,
        ["b", "a"],
    )

    await _assert_rejected("ALTER TABLE x ADD y INT", "unsupported_statement")
    await _assert_rejected("CREATE VIEW x AS SELECT 1", "unsupported_statement")
    await _assert_rejected("CREATE TABLE x (", "malformed_ddl")
    await _assert_rejected(
        "CREATE TABLE x (a INT, A VARCHAR(2))",
        "duplicate_column",
    )
    await _assert_rejected(
        "CREATE TABLE x (a INT); CREATE TABLE X (a INT)",
        "duplicate_table",
    )
    await _assert_rejected(
        "CREATE TABLE x (a INT)",
        "missing_primary_key",
    )

    tiny_limits = app_config.api.model_copy(
        update={"max_ddl_bytes": 8, "max_tables": 1, "max_columns": 1}
    )
    try:
        await parse_ddl("test_source", DDL, tiny_limits)
    except DataAgentError as error:
        check_exception("test_ddl_parser 捕获预期异常", error, DataAgentError)
        check_equal(
            "test_ddl_parser 检查点 10",
            error.code,
            "ddl_too_large",
        )
    else:
        fail_check(
            "test_ddl_parser", actual="未抛出预期异常", expected="DDL 字节限制必须生效"
        )


async def test_parse_ddl_runs_complete_pipeline_off_event_loop(
    monkeypatch: MonkeyPatch,
) -> None:
    """通过线程事件证明解析期间事件循环仍可确定性推进。"""
    original_parse = sqlglot.parse
    entered = threading.Event()
    release = threading.Event()
    parser_thread_ids: list[int] = []

    def blocking_parse(
        *args: Any,
        **kwargs: Any,
    ) -> list[exp.Expression | None]:
        """记录执行线程并等待测试释放后调用真实解析器。"""
        parser_thread_ids.append(threading.get_ident())
        entered.set()
        release.wait()
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(sqlglot, "parse", blocking_parse)
    loop_thread_id = threading.get_ident()
    parse_task = asyncio.create_task(parse_ddl("thread_boundary", DDL))
    try:
        entered_ok = await asyncio.wait_for(
            asyncio.to_thread(entered.wait),
            timeout=2,
        )
        check_equal("解析线程已进入", entered_ok, True)
        progress = asyncio.Event()
        asyncio.get_running_loop().call_soon(progress.set)
        await asyncio.wait_for(progress.wait(), timeout=2)
        check_condition(
            "事件循环在解析阻塞时继续推进",
            progress.is_set(),
            expected="解析线程等待期间调度 loop callback",
        )
        check_condition(
            "SQLGlot 不在事件循环线程执行",
            parser_thread_ids[0] != loop_thread_id,
            actual=parser_thread_ids[0],
            expected=f"不同于事件循环线程 {loop_thread_id}",
        )
    finally:
        release.set()
    await parse_task
