"""DW 结构同步规划的确定性单元检查。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from tests.helpers.checks import (
    check_condition,
    check_equal,
    check_exception,
    fail_check,
)

from data_sync.models import (
    DesiredColumn,
    DesiredSyncTable,
    primary_key_identity,
)
from data_sync.schema_sync import (
    CurrentColumn,
    CurrentTable,
    DWSchemaSynchronizer,
    SchemaLockUnavailableError,
    is_safe_widening,
    plan_schema_changes,
)
from errors import DataAgentError


def _desired(*, amount_type: str = "DECIMAL(12, 2)") -> DesiredSyncTable:
    return DesiredSyncTable(
        source="local",
        source_schema="business",
        source_table="fact_order",
        target_table="fact_order",
        columns=[
            DesiredColumn(
                id="order_id",
                name="order_id",
                data_type="BIGINT",
                nullable=False,
            ),
            DesiredColumn(
                id="amount",
                name="amount",
                data_type=amount_type,
                nullable=False,
            ),
        ],
        primary_key=["order_id"],
        schema_fingerprint="a" * 64,
        metric_dependency_column_ids=["amount"],
    )


async def test_existing_bit_key_uses_the_ownership_codec() -> None:
    """DW 驱动返回的 BIT 字节主键与回填登记的整数文档保持一致。"""
    desired = _desired()
    desired.columns[0] = DesiredColumn(
        id="order_id", name="order_id", data_type="BIT(8)", nullable=False
    )
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[1, 1])
    target_result = MagicMock()
    target_result.mappings.return_value.all.return_value = [{"order_id": b"\x05"}]
    owner_result = MagicMock()
    session.execute = AsyncMock(side_effect=[target_result, owner_result])

    document, key_hash = primary_key_identity(desired, {"order_id": 5})
    owner_result.__iter__.return_value = iter([(key_hash, document)])

    await DWSchemaSynchronizer(session, database="dw")._validate_existing_provenance(
        desired
    )


async def test_schema_lock_contention_has_a_distinct_error() -> None:
    """命名锁超时必须可由服务识别为不消耗预算的正常竞争。"""
    session = AsyncMock()
    session.scalar.return_value = 0

    with pytest.raises(SchemaLockUnavailableError):
        await DWSchemaSynchronizer(session, database="dw").synchronize(_desired())


async def test_authority_check_runs_inside_schema_lock_and_before_each_ddl() -> None:
    """Authority 首查必须晚于结构锁，并在每条不可逆 DDL 前复查。"""
    desired = _desired()
    events: list[str] = []
    session = AsyncMock()
    owner_connection = AsyncMock()
    session.connection.return_value = owner_connection

    async def scalar(statement: object, parameters: dict[str, object]) -> int:
        sql = str(statement)
        if "GET_LOCK" in sql:
            events.append("schema_lock_acquired")
            return 1
        events.append("schema_lock_released")
        return 1

    async def execute(statement: object) -> None:
        events.append(f"ddl:{str(statement).split(' ', 1)[0]}")

    async def check_authority() -> bool:
        events.append("authority")
        return True

    session.scalar.side_effect = scalar
    session.execute.side_effect = execute
    synchronizer = DWSchemaSynchronizer(session, database="dw")
    synchronizer.inspect = AsyncMock(
        side_effect=[
            None,
            CurrentTable(
                columns=tuple(
                    CurrentColumn(
                        name=column.name,
                        data_type=column.data_type,
                        nullable=column.nullable,
                    )
                    for column in desired.columns
                ),
                primary_key=tuple(desired.primary_key),
            ),
        ]
    )
    synchronizer._shared_columns = AsyncMock(return_value=(set(), set()))

    await synchronizer.synchronize(desired, check_authority=check_authority)

    check_equal(
        "结构锁、authority、DDL 与释放顺序",
        events,
        [
            "schema_lock_acquired",
            "authority",
            "authority",
            "ddl:CREATE",
            "schema_lock_released",
        ],
    )


async def test_initial_authority_loss_releases_schema_lock_without_inspection() -> None:
    """结构锁内首查失效时必须释放锁且不得继续读取或执行 DDL。"""
    events: list[str] = []
    session = AsyncMock()
    session.connection.return_value = AsyncMock()

    async def scalar(statement: object, parameters: dict[str, object]) -> int:
        events.append("get" if "GET_LOCK" in str(statement) else "release")
        return 1

    session.scalar.side_effect = scalar
    synchronizer = DWSchemaSynchronizer(session, database="dw")
    synchronizer.inspect = AsyncMock()

    with pytest.raises(RuntimeError, match="generation 已失效"):
        await synchronizer.synchronize(
            _desired(),
            check_authority=AsyncMock(return_value=False),
        )

    check_equal("失效 authority 的锁清理顺序", events, ["get", "release"])
    check_equal("失效 authority 不读取结构", synchronizer.inspect.await_count, 0)
    check_equal("失效 authority 不执行 DDL", session.execute.await_count, 0)


async def test_existing_table_requires_ownership_for_every_row() -> None:
    """非空目标表中只要存在未登记行就拒绝开始同步。"""
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[2, 1])
    synchronizer = DWSchemaSynchronizer(session, database="dw")

    with pytest.raises(DataAgentError, match="未登记或多余"):
        await synchronizer._validate_existing_provenance(_desired())


async def test_existing_table_rejects_equal_count_misaligned_ownership() -> None:
    """DW 与 ownership 数量相等但主键集合错位时仍拒绝接管。"""
    session = AsyncMock()
    session.scalar = AsyncMock(side_effect=[2, 2])
    target_result = MagicMock()
    target_result.mappings.return_value.all.return_value = [
        {"order_id": 1},
        {"order_id": 2},
    ]
    owner_result = [("missing", "{}")]
    session.execute = AsyncMock(side_effect=[target_result, owner_result])

    with pytest.raises(DataAgentError, match="主键错位"):
        await DWSchemaSynchronizer(
            session, database="dw"
        )._validate_existing_provenance(_desired())


async def test_provenance_snapshot_commits_before_existing_table_ddl() -> None:
    """一致性读取事务必须在另一连接执行 ALTER TABLE 前结束。"""
    desired = _desired()
    session = AsyncMock()
    session.connection.return_value = AsyncMock()
    session.scalar.side_effect = [1, 1]
    provenance = AsyncMock()
    events: list[str] = []
    provenance.commit.side_effect = lambda: events.append("snapshot_commit")
    session.execute.side_effect = lambda statement: events.append(f"ddl:{statement}")
    synchronizer = DWSchemaSynchronizer(session, database="dw").with_provenance_session(
        provenance
    )
    synchronizer.inspect = AsyncMock(
        side_effect=[
            CurrentTable(
                columns=(
                    CurrentColumn(name="order_id", data_type="BIGINT", nullable=False),
                ),
                primary_key=("order_id",),
            ),
            CurrentTable(
                columns=tuple(
                    CurrentColumn(
                        name=column.name,
                        data_type=column.data_type,
                        nullable=column.nullable,
                    )
                    for column in desired.columns
                ),
                primary_key=tuple(desired.primary_key),
            ),
        ]
    )
    synchronizer._shared_columns = AsyncMock(return_value=(set(), set()))
    synchronizer._validate_existing_provenance = AsyncMock()

    await synchronizer.synchronize(desired)

    check_condition("快照在 DDL 前结束", events[0] == "snapshot_commit")


def test_plan_creates_missing_table_and_quotes_identifiers() -> None:
    """缺失目标表时生成一条带主键的安全 CREATE TABLE。"""
    statements = plan_schema_changes(
        database="dw",
        desired=_desired(),
        current=None,
    )
    check_equal("缺失目标表只生成一条 DDL", len(statements), 1)
    check_condition(
        "CREATE TABLE 引用数据库和表名",
        statements[0].startswith("CREATE TABLE dw.fact_order"),
        actual=statements[0],
    )
    check_condition(
        "CREATE TABLE 包含已接受主键",
        "PRIMARY KEY (order_id)" in statements[0],
        actual=statements[0],
    )
    check_condition(
        "字符串主键使用字节身份一致的 NO PAD 排序规则",
        "COLLATE utf8mb4_0900_bin" in statements[0],
        actual=statements[0],
    )


def test_plan_creates_shared_column_with_merged_nullable_contract() -> None:
    """任一共享来源允许 NULL 时首次建表使用可空安全超集。"""
    statements = plan_schema_changes(
        database="dw",
        desired=_desired(),
        current=None,
        shared_nullable_columns={"amount"},
    )

    check_condition(
        "共享列合并为 nullable",
        "amount DECIMAL(12, 2) NULL" in statements[0],
        actual=statements[0],
    )


async def test_shared_columns_merge_peer_nullable_contract() -> None:
    """共享来源的同名 nullable 列会进入目标安全超集。"""
    peer = _desired()
    peer.source = "peer"
    peer.columns[1] = DesiredColumn(
        id="amount", name="amount", data_type="DECIMAL(12, 2)", nullable=True
    )
    result = MagicMock()
    result.scalars.return_value = [peer.model_dump()]
    session = AsyncMock()
    session.execute.return_value = result

    extra, nullable = await DWSchemaSynchronizer(
        session, database="dw"
    )._shared_columns(_desired())

    check_equal("没有额外共享列", extra, set())
    check_equal("同名列可空性取安全超集", nullable, {"amount"})


def test_boolean_alias_matches_mysql_introspection() -> None:
    """MySQL 将 BOOLEAN introspect 为 tinyint(1) 后仍保持幂等。"""
    desired = _desired(amount_type="BOOLEAN")
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "tinyint(1)", False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "BOOLEAN 与 tinyint(1) 等价",
        plan_schema_changes(database="dw", desired=desired, current=current),
        [],
    )


@pytest.mark.parametrize(
    ("desired_type", "current_type"),
    [
        ("DECIMAL", "decimal(10,0)"),
        ("DECIMAL(10)", "decimal(10,0)"),
        ("DECIMAL UNSIGNED", "decimal(10,0) unsigned"),
        ("DECIMAL(12) UNSIGNED", "decimal(12,0) unsigned"),
    ],
)
def test_decimal_defaults_match_mysql_introspection(
    desired_type: str, current_type: str
) -> None:
    """MySQL 补全 DECIMAL 默认精度和 scale 后仍保持幂等。"""
    desired = _desired(amount_type=desired_type)
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", current_type, False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "DECIMAL 默认参数等价",
        plan_schema_changes(database="dw", desired=desired, current=current),
        [],
    )


def test_plan_accepts_nullable_columns_from_other_sources() -> None:
    """共享目标中其他来源贡献的 nullable 字段不阻塞当前来源。"""
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(12,2)", False),
            CurrentColumn("crm_note", "varchar(100)", True),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "其他来源 nullable 字段可安全省略",
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=current,
            compatible_extra_columns={"crm_note"},
        ),
        [],
    )


def test_plan_rejects_required_columns_from_other_sources() -> None:
    """共享目标中其他来源的必填字段不能由当前来源安全省略。"""
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(12,2)", False),
            CurrentColumn("required_value", "int", False),
        ),
        primary_key=("order_id",),
    )
    with pytest.raises(DataAgentError, match="待删除字段"):
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=current,
            compatible_extra_columns=set(),
        )


@pytest.mark.parametrize(
    ("current", "reason"),
    [
        (
            CurrentTable(
                columns=(CurrentColumn("order_id", "bigint", False),),
                primary_key=("order_id",),
                engine="MyISAM",
            ),
            "InnoDB",
        ),
        (
            CurrentTable(
                columns=(CurrentColumn("order_id", "bigint", False),),
                primary_key=("order_id",),
                unique_indexes=("uq_email",),
            ),
            "额外唯一索引",
        ),
    ],
)
def test_plan_rejects_non_transactional_or_extra_unique_constraints(
    current: CurrentTable,
    reason: str,
) -> None:
    """已有目标表必须保持事务原子性和仅主键冲突语义。"""
    try:
        plan_schema_changes(database="dw", desired=_desired(), current=current)
    except DataAgentError as error:
        check_equal("不兼容目标表错误码", error.code, "dw_schema_conflict")
        check_condition("错误说明具体不兼容项", reason in str(error), actual=str(error))
    else:
        fail_check("不兼容目标表被拒绝", actual=current, expected=reason)


def test_plan_adds_and_safely_widens_columns() -> None:
    """已有表只添加缺失列或扩大被支持的字段类型。"""
    missing_column = CurrentTable(
        columns=(CurrentColumn("order_id", "bigint", False),),
        primary_key=("order_id",),
    )
    check_equal(
        "缺失列生成 ADD COLUMN",
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=missing_column,
        ),
        ["ALTER TABLE dw.fact_order ADD COLUMN amount DECIMAL(12, 2) NOT NULL"],
    )
    narrow_decimal = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(10,2)", False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "安全扩大 DECIMAL 生成 MODIFY COLUMN",
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=narrow_decimal,
        ),
        ["ALTER TABLE dw.fact_order MODIFY COLUMN amount DECIMAL(12, 2) NOT NULL"],
    )

    already_wider = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(18,4)", False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "共享目标已更宽时无需缩窄",
        plan_schema_changes(database="dw", desired=_desired(), current=already_wider),
        [],
    )


def test_plan_accepts_nullable_target_for_required_source_column() -> None:
    """目标允许 NULL 是来源 NOT NULL 契约的安全超集。"""
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(12,2)", True),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "更宽松目标可空性无需修改",
        plan_schema_changes(database="dw", desired=_desired(), current=current),
        [],
    )


def test_plan_relaxes_required_target_for_later_nullable_source() -> None:
    """后发布的 nullable 来源可安全放宽既有 NOT NULL 共享列。"""
    desired = _desired()
    desired.columns[1] = DesiredColumn(
        id="amount", name="amount", data_type="DECIMAL(12, 2)", nullable=True
    )
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(12,2)", False),
        ),
        primary_key=("order_id",),
    )

    check_equal(
        "后到 nullable 契约生成安全放宽 DDL",
        plan_schema_changes(database="dw", desired=desired, current=current),
        ["ALTER TABLE dw.fact_order MODIFY COLUMN amount DECIMAL(12, 2) NULL"],
    )


def test_plan_preserves_nullable_target_when_widening_required_column() -> None:
    """目标扩宽类型时不收紧已有的可空性。"""
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(10,2)", True),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        "扩宽类型保留目标可空性",
        plan_schema_changes(database="dw", desired=_desired(), current=current),
        ["ALTER TABLE dw.fact_order MODIFY COLUMN amount DECIMAL(12, 2) NULL"],
    )


def test_plan_rejects_non_binary_string_primary_key_collation() -> None:
    """已有字符串主键必须与字节 ownership 使用相同等价语义。"""
    desired = _desired()
    desired.columns[0] = DesiredColumn(
        id="order_id", name="order_id", data_type="VARCHAR(64)", nullable=False
    )
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "varchar(64)", False, "utf8mb4_general_ci"),
            CurrentColumn("amount", "decimal(12,2)", False),
        ),
        primary_key=("order_id",),
    )
    with pytest.raises(DataAgentError, match="utf8mb4_0900_bin"):
        plan_schema_changes(database="dw", desired=desired, current=current)


def test_enum_literals_keep_original_case() -> None:
    """生成 DDL 时不改写 ENUM 业务字面值。"""
    desired = _desired(amount_type="ENUM('pending','Done')")
    statements = plan_schema_changes(database="dw", desired=desired, current=None)
    check_condition(
        "ENUM 字面量保留大小写",
        "'pending'" in statements[0] and "'Done'" in statements[0],
        actual=statements[0],
    )


@pytest.mark.parametrize(
    ("current", "desired", "expected"),
    [
        ("INT", "BIGINT", True),
        ("INT UNSIGNED", "BIGINT", True),
        ("BIGINT UNSIGNED", "BIGINT", False),
        ("VARCHAR(32)", "VARCHAR(64)", True),
        ("VARBINARY(64)", "VARBINARY(32)", False),
        ("DECIMAL(10,2)", "DECIMAL(12,3)", True),
        ("DECIMAL(10,2)", "DECIMAL(12,2) UNSIGNED", False),
        ("DECIMAL(10,2) UNSIGNED", "DECIMAL(12,2)", True),
        ("DECIMAL(10,2)", "DECIMAL(10,3)", False),
    ],
)
def test_safe_widening_matrix(current: str, desired: str, expected: bool) -> None:
    """安全扩宽矩阵覆盖整数、变长字段和定点数边界。"""
    check_equal(
        f"{current} -> {desired}",
        is_safe_widening(current, desired),
        expected,
    )


@pytest.mark.parametrize("data_type", ["DATETIME(0)", "TIMESTAMP(0)", "TIME(0)"])
def test_zero_temporal_precision_matches_mysql_introspection(data_type: str) -> None:
    """显式零 FSP 与 MySQL 省略零 FSP 的结构表示等价。"""
    desired = _desired(amount_type=data_type)
    current = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", data_type.split("(", 1)[0].lower(), False),
        ),
        primary_key=("order_id",),
    )
    check_equal(
        f"{data_type} 复核收敛",
        plan_schema_changes(database="dw", desired=desired, current=current),
        [],
    )


def test_plan_rejects_destructive_difference() -> None:
    """额外字段、主键变化和缩窄类型均成为非重试结构冲突。"""
    destructive = CurrentTable(
        columns=(
            CurrentColumn("order_id", "bigint", False),
            CurrentColumn("amount", "decimal(18,4)", False),
            CurrentColumn("legacy", "varchar(32)", True),
        ),
        primary_key=("order_id",),
    )
    try:
        plan_schema_changes(
            database="dw",
            desired=_desired(),
            current=destructive,
        )
    except Exception as error:
        check_exception("破坏性结构差异被拒绝", error, DataAgentError)
        check_equal(
            "结构冲突不可重试",
            getattr(error, "retryable", None),
            False,
        )
    else:
        fail_check(
            "破坏性结构差异被拒绝",
            actual="未抛出异常",
            expected="DataAgentError",
        )
