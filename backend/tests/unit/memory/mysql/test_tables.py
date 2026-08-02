"""锁定长期记忆 Core 元数据与 bootstrap DDL 的关键约束。"""

from pathlib import Path

from memory.mysql.tables import (
    agent_memory,
    agent_memory_event,
    agent_memory_link,
    memory_index_outbox,
)
from settings import app_config
from tests.helpers.checks import check_condition, check_equal


def test_memory_table_indexes_match_bootstrap_contract() -> None:
    """Core create_all 必须生成 bootstrap 声明的查询索引。"""
    check_equal(
        "agent_memory 索引",
        {index.name for index in agent_memory.indexes},
        {
            "idx_agent_memory_exact",
            "idx_agent_memory_rebuild",
            "idx_agent_memory_user",
            "idx_agent_memory_expiry",
            "idx_agent_memory_text_hash",
            "idx_agent_memory_key_lookup",
        },
    )
    check_equal(
        "agent_memory_event 索引",
        {index.name for index in agent_memory_event.indexes},
        {"idx_agent_memory_event_history"},
    )
    check_equal(
        "memory_index_outbox 索引",
        {index.name for index in memory_index_outbox.indexes},
        {"idx_memory_index_outbox_claim"},
    )


def test_memory_table_foreign_keys_match_bootstrap_contract() -> None:
    """历史、关联和投影 outbox 不得指向不存在的权威记忆。"""
    database = app_config.memory.database
    foreign_keys = {
        (
            foreign_key.parent.table.name,
            foreign_key.parent.name,
            foreign_key.target_fullname,
            (
                foreign_key.constraint.name
                if foreign_key.constraint is not None
                else None
            ),
        )
        # memory_index_outbox 刻意不设外键：见 tables.py 中的说明。
        for table in (agent_memory_event, agent_memory_link)
        for foreign_key in table.foreign_keys
    }
    check_equal(
        "长期记忆外键",
        foreign_keys,
        {
            (
                "agent_memory_event",
                "memory_id",
                f"{database}.agent_memory.id",
                "fk_agent_memory_event_memory",
            ),
            (
                "agent_memory_link",
                "memory_id",
                f"{database}.agent_memory.id",
                "fk_agent_memory_link_memory",
            ),
            (
                "agent_memory_link",
                "linked_memory_id",
                f"{database}.agent_memory.id",
                "fk_agent_memory_link_linked",
            ),
        },
    )


def _bootstrap_agent_memory_block() -> str:
    """从 bootstrap 脚本中截取 agent_memory 的 CREATE TABLE 定义。"""
    script = (
        Path(__file__).parents[5]
        / "docs"
        / "docker"
        / "mysql"
        / "data_agent.sql"
    ).read_text(encoding="utf-8")
    start = script.index("CREATE TABLE IF NOT EXISTS agent_memory")
    return script[start : script.index("ENGINE = InnoDB", start)]


def _bootstrap_columns(block: str) -> set[str]:
    """解析 CREATE TABLE 块中声明的列名。

    按顶层逗号切分而不是按行切分：列定义可以跨行（例如 updated_at 的
    ``ON UPDATE CURRENT_TIMESTAMP`` 换行续写），逐行解析会把续写行的首个词误当列名。
    """
    # 步骤一：去掉 CREATE TABLE 前缀，只保留最外层括号内的定义体。
    body = block[block.index("(") + 1 :]
    # 步骤二：按嵌套深度为零的逗号切分，使类型参数与索引列表内的逗号不参与切分。
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    # 步骤三：跳过表级约束，取每个列定义的首个标识符。
    skipped = ("INDEX", "PRIMARY", "UNIQUE", "CONSTRAINT", "FOREIGN", "KEY")
    columns: set[str] = set()
    for part in parts:
        definition = part.strip()
        if not definition or definition.startswith(skipped):
            continue
        name = definition.split()[0]
        if name.isidentifier():
            columns.add(name)
    return columns


def test_memory_core_columns_match_bootstrap_script() -> None:
    """Core 定义与 bootstrap 脚本是两份来源，列集合必须逐一对应。

    仓库没有升级迁移机制，bootstrap 脚本是新环境建表的唯一依据；两处任一漏改都会
    让新环境与 ORM 期望的结构不一致，且只有真正连库时才暴露。
    """
    block = _bootstrap_agent_memory_block()
    check_equal(
        "agent_memory 列集合",
        {column.name for column in agent_memory.columns},
        _bootstrap_columns(block),
    )
    check_condition(
        "文本哈希列参与精确基线索引",
        "idx_agent_memory_text_hash" in block and "memory_text_hash" in block,
        actual=block,
        expected="bootstrap 声明 memory_text_hash 列与其索引",
    )
