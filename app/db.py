"""数据库方言与连接管理：SQLite（默认）与 PostgreSQL（Neon）的差异**全部集中在这里**。

为什么要单独一层
----------------
``app/store.py`` 里几十个方法写的是 SQLite 的原生 SQL（``?`` 占位符、
``INSERT OR REPLACE``、``PRAGMA``、``sqlite3.Row``）。要让同一份代码跑在 Postgres 上，
最忌讳的做法是在每个方法里写 ``if postgres:`` —— 那样维护者再也说不清"两种库到底差在哪"。

本模块把差异收敛成三类，store 的方法体**一行都不用改**：

1. **SQL 文本翻译**：``?`` → ``%s``、``INSERT OR REPLACE`` → ``ON CONFLICT ... DO UPDATE``、
   ``INSERT OR IGNORE`` → ``ON CONFLICT DO NOTHING``、``LIKE`` → ``ILIKE``（见下）。
2. **DDL 翻译**：``INTEGER PRIMARY KEY AUTOINCREMENT`` → identity 列、``REAL`` → ``DOUBLE PRECISION``。
3. **连接与元数据**：SQLite 每次操作一条短连接；Postgres 走进程内共享连接池，
   并用 ``information_schema`` 替代 ``PRAGMA table_info``。

关于 LIKE
--------
SQLite 的 ``LIKE`` 对 ASCII 默认**不区分大小写**，Postgres 的 ``LIKE`` 区分大小写。
管理端的 run/session 搜索是按用户输入模糊匹配，直接照搬会让 Neon 上突然"搜不到"，
所以翻译成 ``ILIKE`` 以保留 SQLite 的检索手感（这是唯一一处语义对齐，特此注明）。

关于类型
--------
- JSON 一律继续存 ``TEXT``（本仓所有写入点都先 ``json.dumps``），不上 ``jsonb``：
  上 jsonb 就必须改所有写入/读取点，且 SQLite 侧无法对等，得不偿失。
- 布尔继续用 ``INTEGER 0/1``，**不用** ``BOOLEAN``：读回来要保持 ``0/1``，
  消费方（``bool(row["amap_verified"])`` 等）依赖这个形状。
- ``REAL`` 在 SQLite 是 8 字节浮点、在 Postgres 是 4 字节；经纬度会掉精度，
  所以 DDL 翻译成 ``DOUBLE PRECISION``。

关于"绝不静默回退"
----------------
选了 Postgres 却连不上时，**抛清晰的异常**（信息里带 ``postgres`` / ``neon`` 字样），
绝不悄悄退回 SQLite 临时文件 —— 那会让线上以为在写 Neon，实际写进了容器本地盘。
"""

from __future__ import annotations

import atexit
import contextlib
import re
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

# ----------------------------------------------------------------------
# 库位置（与旧版 app/store.py 完全一致，原位搬迁以便 db 层自描述）
# ----------------------------------------------------------------------

#: 默认库位置：项目根 /data/travelplan.db（PRD §17）。
DEFAULT_DB_PATH = Path(__file__).resolve().parents[1] / "data" / "travelplan.db"

#: 覆盖 SQLite 库位置的环境变量。
#: 为什么必须有：容器/系统服务里"代码目录"通常是只读或易失的，真实数据必须落在挂载卷上。
DB_PATH_ENV = "TRAVELPLAN_DB_PATH"


def default_db_path() -> Path:
    """当前生效的默认 SQLite 库路径（环境变量优先）。"""

    import os

    raw = os.environ.get(DB_PATH_ENV, "").strip()
    return Path(raw) if raw else DEFAULT_DB_PATH


class DialectError(RuntimeError):
    """SQL 无法翻译成目标方言（例如 INSERT OR REPLACE 的目标表没登记主键）。"""


class PostgresUnavailable(RuntimeError):
    """Postgres / Neon 不可用。

    **不要**在任何地方捕获它然后回退 SQLite：这会把"线上其实没连上 Neon"变成静默的数据丢失。
    """


# ----------------------------------------------------------------------
# SQL 翻译
# ----------------------------------------------------------------------

#: ``INSERT OR REPLACE`` 的目标表 → 冲突键（= 该表主键）。
#:
#: SQLite 的 REPLACE 隐式按主键/唯一键冲突覆盖；Postgres 必须显式给出
#: ``ON CONFLICT (<pk>) DO UPDATE SET ...`` 的目标。这里是**唯一**需要维护的映射：
#: 新增带 INSERT OR REPLACE 的表时，只要在这里加一行。
INSERT_OR_REPLACE_PK: dict[str, tuple[str, ...]] = {
    "runs": ("run_id",),
    "run_progress": ("run_id",),
    "run_metrics": ("run_id",),
    "trace_spans": ("span_id",),
    "sources": ("source_id",),
    "evidence": ("evidence_id",),
    "places": ("run_id", "place_id"),
    "plans": ("run_id",),
    "badcases": ("badcase_id",),
    "benchmark_runs": ("benchmark_run_id",),
    "benchmark_case_results": ("result_id",),
    "evolution_runs": ("evolution_run_id",),
    "experiences": ("experience_id",),
    "planning_sessions": ("session_id",),
    "provider_calls": ("call_id",),
    "runtime_config": ("key",),
}

_INSERT_OR_RE = re.compile(
    r"INSERT\s+OR\s+(?P<kind>REPLACE|IGNORE)\s+INTO\s+"
    r"(?P<table>[A-Za-z_][A-Za-z0-9_]*)\s*\((?P<cols>[^)]*)\)",
    re.IGNORECASE,
)
#: ``\bLIKE\b``：只命中 SQL 关键字，不碰参数值。
_LIKE_RE = re.compile(r"\bLIKE\b", re.IGNORECASE)
_AUTOINCREMENT_RE = re.compile(r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT", re.IGNORECASE)
_REAL_RE = re.compile(r"\bREAL\b", re.IGNORECASE)


def translate_insert_or_replace(sql: str) -> str:
    """把 SQLite 的 ``INSERT OR REPLACE/IGNORE`` 改写成离线等价的 Postgres 语句。

    改写分两步：先把 ``INSERT OR REPLACE INTO t (cols)`` 头换成 ``INSERT INTO t (cols)``，
    再把 ``ON CONFLICT ...`` 子句追加到**整条语句末尾** —— 这样 ``VALUES (...)`` 与
    ``INSERT ... SELECT ...`` 两种形式都能正确收尾。
    """

    matches = list(_INSERT_OR_RE.finditer(sql))
    if not matches:
        return sql
    if len(matches) > 1:
        raise DialectError("一条 SQL 里出现多个 INSERT OR REPLACE/IGNORE，翻译器不支持")

    match = matches[0]
    kind = match.group("kind").upper()
    table = match.group("table")
    columns = [column.strip() for column in match.group("cols").split(",") if column.strip()]

    head = f"INSERT INTO {table} ({match.group('cols')})"
    if kind == "IGNORE":
        # INSERT OR IGNORE 忽略**任意**唯一冲突，所以不加冲突目标。
        clause = " ON CONFLICT DO NOTHING"
    else:
        primary_key = INSERT_OR_REPLACE_PK.get(table.lower())
        if not primary_key:
            raise DialectError(
                f"表 {table} 用了 INSERT OR REPLACE，但没有登记主键，"
                "无法生成 ON CONFLICT 目标（请补进 INSERT_OR_REPLACE_PK）"
            )
        updates = [column for column in columns if column not in primary_key]
        if updates:
            assignments = ", ".join(f"{column}=EXCLUDED.{column}" for column in updates)
            clause = f" ON CONFLICT ({', '.join(primary_key)}) DO UPDATE SET {assignments}"
        else:
            clause = " ON CONFLICT DO NOTHING"

    rewritten = sql[: match.start()] + head + sql[match.end() :]
    return rewritten.rstrip().rstrip(";") + clause


def to_postgres_sql(sql: str, *, bind: bool) -> str:
    """把 SQLite 方言的 SQL 整条翻译成 Postgres 可执行的 SQL。

    ``bind=True`` 表示这次会带参数（psycopg 用 ``%s`` 风格，字面 ``%`` 必须写成 ``%%``）。
    先转义 ``%`` 再把 ``?`` 换成 ``%s``，顺序不能反，否则会把自己的 ``%s`` 也转义掉。
    """

    translated = translate_insert_or_replace(sql)
    if bind:
        translated = translated.replace("%", "%%")
    translated = translated.replace("?", "%s")
    return _LIKE_RE.sub("ILIKE", translated)


def translate_postgres_schema(sql: str) -> str:
    """把共享的 DDL 翻译成 Postgres 可执行版本（只动类型，不动表结构）。"""

    translated = _AUTOINCREMENT_RE.sub(
        "BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY", sql
    )
    # SQLite 的 REAL 是 8 字节，Pg 的 REAL 只有 4 字节；经纬度/成本会掉精度。
    return _REAL_RE.sub("DOUBLE PRECISION", translated)


def split_sql_statements(sql: str) -> list[str]:
    """把一段多语句脚本拆成单条语句（供 Postgres 逐条执行）。

    psycopg 只在"无参数"时支持一次发多条，逐条执行更可控；本项目 DDL 里没有
    字符串字面量包含 ``;``，所以按行去掉 ``--`` 注释后按 ``;`` 切分即可。
    """

    without_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    return [statement.strip() for statement in without_comments.split(";") if statement.strip()]


def mask_dsn(dsn: str) -> str:
    """把连接串里的凭据脱敏成 ``postgresql://***@host/db``（用户名/密码绝不回显）。"""

    parts = urlsplit(dsn)
    host = parts.hostname or ""
    database = (parts.path or "").lstrip("/")
    return f"postgresql://***@{host}/{database}"


def _execute_raw(conn: Any, sql: str, params: Any = None) -> Any:
    """在"原生"通道上执行 SQL（不做 ``?``/``%`` 翻译）。

    方言自省（PRAGMA / information_schema）自己就写的是目标方言的 SQL，
    再走一遍翻译会把 ``%s`` 转义坏。Postgres 包装类暴露 ``raw_execute`` 走这里。
    """

    raw = getattr(conn, "raw_execute", None)
    if callable(raw):
        return raw(sql, params)
    if params is None:
        return conn.execute(sql)
    return conn.execute(sql, params)


# ----------------------------------------------------------------------
# 连接池（Postgres）
# ----------------------------------------------------------------------
#: 为什么不用"每次操作一条短连接"：Neon 建连有开销，而且会占用连接数配额。
#: 这里维护一个**进程内共享**的懒初始化池（按连接串复用），最多 ``DEFAULT_MAX_CONNECTIONS`` 条。
DEFAULT_MAX_CONNECTIONS = 4
_POOLS: dict[str, "_ConnectionPool"] = {}
_POOLS_LOCK = threading.Lock()


class _ConnectionPool:
    """极简固定上限连接池：只用一个锁 + 条件变量，**不起后台线程**。

    不起后台线程是有意的：进程退出时没有需要 join 的工作线程，``close()`` 也只是关掉
    空闲连接，永远不会把退出卡住（API 常驻进程无所谓，短命脚本/测试也能干净收尾）。
    """

    def __init__(self, conninfo: str, max_size: int = DEFAULT_MAX_CONNECTIONS) -> None:
        self._conninfo = conninfo
        self._max_size = max(1, int(max_size))
        self._idle: list[Any] = []
        self._live = 0
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._closed = False

    def acquire(self, timeout: float = 15.0) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            create = False
            stale: list[Any] = []
            with self._cond:
                if self._closed:
                    raise PostgresUnavailable("postgres/neon 连接池已关闭，无法再获取连接")
                while self._idle:
                    conn = self._idle.pop()
                    if getattr(conn, "closed", False) or getattr(conn, "broken", False):
                        self._live -= 1
                        stale.append(conn)  # 坏连接不能留着，也不能在持锁时关
                        continue
                    for dead in stale:
                        with contextlib.suppress(Exception):
                            dead.close()
                    return conn
                if self._live < self._max_size:
                    self._live += 1
                    create = True
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PostgresUnavailable(
                            "等待 postgres/neon 连接超时（连接池已满且长时间未归还）"
                        )
                    self._cond.wait(remaining)
            for dead in stale:
                with contextlib.suppress(Exception):
                    dead.close()
            if not create:
                continue
            try:
                return self._connect()
            except PostgresUnavailable:
                # 建连失败必须把刚才预占的名额还回去。否则 DATABASE_URL 配了但连不上时，
                # 每失败一次 `_live` 就多欠一条：第 N+1 次调用会以为"池子满了"，
                # 于是从"2 秒清晰报错"退化成"干等 15 秒超时" —— 线上排查方向会被带偏，
                # 而且 Neon 恢复后池子仍然被这些幽灵名额占着，永远连不上。
                with self._cond:
                    self._live -= 1
                    self._cond.notify()
                raise
            except Exception as exc:  # noqa: BLE001 —— 统一转成清晰异常，绝不回退 SQLite
                with self._cond:
                    self._live -= 1
                    self._cond.notify()
                raise PostgresUnavailable(
                    f"无法连接 postgres（Neon）：{type(exc).__name__}: {exc}"
                ) from exc

    def _connect(self) -> Any:
        try:
            import psycopg  # 延迟导入：只用 SQLite 时无需安装驱动
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover —— 有测试进程依赖时才可能触发
            raise PostgresUnavailable(
                "检测到 DATABASE_URL 但未安装 psycopg（Neon/postgres 驱动）："
                "pip install 'psycopg[binary]'"
            ) from exc
        # rows 用 dict_row：与 sqlite3.Row 一样支持 row["c"] / row.keys()，但可直接 dict()。
        return psycopg.connect(self._conninfo, row_factory=dict_row)

    def release(self, conn: Any) -> None:
        if conn is None:
            return
        discard = getattr(conn, "closed", False) or getattr(conn, "broken", False)
        with self._cond:
            if self._closed:
                discard = True
            if discard:
                self._live = max(0, self._live - 1)
            else:
                self._idle.append(conn)
            self._cond.notify()
        if discard:
            with contextlib.suppress(Exception):
                conn.close()

    def close(self) -> None:
        """关闭池并释放所有空闲连接（非阻塞：不 join 任何线程）。"""

        with self._cond:
            self._closed = True
            idle, self._idle = self._idle, []
            self._live = max(0, self._live - len(idle))
            self._cond.notify_all()
        for conn in idle:
            with contextlib.suppress(Exception):
                conn.close()


def get_pool(conninfo: str) -> _ConnectionPool:
    """按连接串取（或建）进程内共享的连接池。懒初始化，不做任何网络连接。"""

    with _POOLS_LOCK:
        pool = _POOLS.get(conninfo)
        if pool is None:
            pool = _ConnectionPool(conninfo)
            _POOLS[conninfo] = pool
        return pool


def close_all_pools() -> None:
    """关闭所有连接池。测试收尾与进程退出时调用；幂等。"""

    with _POOLS_LOCK:
        pools = list(_POOLS.values())
        _POOLS.clear()
    for pool in pools:
        pool.close()


atexit.register(close_all_pools)


# ----------------------------------------------------------------------
# Postgres 连接包装
# ----------------------------------------------------------------------


class PostgresConnection:
    """把 store 里写的 SQLite SQL 透明翻译成 Postgres 执行的连接包装。

    store 的用法 ``with self._connect() as conn:`` 与 SQLite 完全一致：
    进入时从池里取一条连接，退出时 commit/rollback 并归还。
    """

    def __init__(self, pool: _ConnectionPool) -> None:
        self._pool = pool
        self._conn = pool.acquire()

    # -- DB-API 形状（store 会用到 execute / executemany / executescript）--

    def execute(self, sql: str, params: Any = None) -> Any:
        return self._conn.execute(to_postgres_sql(sql, bind=params is not None), params)

    def executemany(self, sql: str, seq_of_params: Any) -> Any:
        cursor = self._conn.cursor()
        cursor.executemany(to_postgres_sql(sql, bind=True), seq_of_params)
        return cursor

    def executescript(self, sql: str) -> None:
        # 调用方已通过 backend.schema_ddl() 翻译过 DDL；这里只负责拆条执行。
        for statement in split_sql_statements(sql):
            self._conn.execute(statement)

    def raw_execute(self, sql: str, params: Any = None) -> Any:
        """方言自省专用：SQL 已经是 Postgres 写法，跳过翻译。"""

        return self._conn.execute(sql, params)

    # -- 上下文管理 --

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        conn, self._conn = self._conn, None
        try:
            if conn is None:
                return False
            if exc_type is None:
                conn.commit()
            else:
                with contextlib.suppress(Exception):
                    conn.rollback()
        finally:
            self._pool.release(conn)
        return False


# ----------------------------------------------------------------------
# Backend：连接 + 方言自省 + 自描述
# ----------------------------------------------------------------------


class SqliteBackend:
    """SQLite 后端：行为与历史版本**逐字节一致**（每次操作一条短连接、sqlite3.Row）。"""

    name = "sqlite"

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        # 外键约束打开：evidence → sources、place_evidence → places 的引用完整性
        # 靠数据库保证，而不是靠调用方自觉。
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def schema_ddl(self, schema: str) -> str:
        return schema

    def table_columns(self, conn: Any, table: str) -> set[str]:
        return {row["name"] for row in _execute_raw(conn, f"PRAGMA table_info({table})")}

    def table_primary_key(self, conn: Any, table: str) -> tuple[str, ...]:
        rows = _execute_raw(conn, f"PRAGMA table_info({table})").fetchall()
        ordered = sorted((row["pk"], row["name"]) for row in rows if row["pk"])
        return tuple(name for _, name in ordered)

    def describe(self) -> dict[str, Any]:
        return {"backend": "sqlite", "path": str(self.path)}

    def ping(self) -> None:
        """一条最轻的连通性验证（不做任何 DDL）。

        健康检查以前直接调 `init_schema()`，在远端库上是二十多条 `CREATE TABLE IF NOT
        EXISTS` 走网络 —— Render 的健康检查只给 5 秒，Neon 冷启动时必然超时，
        于是**部署被判定为不健康而回滚**（实测 update_failed：
        "HTTP health check failed (timed out after 5 seconds)"）。
        建表本来就在 store 构造时做过一次，健康检查只需要回答"库还连得上吗"。
        """

        conn = self.connect()
        try:
            _execute_raw(conn, "SELECT 1").fetchone()
        finally:
            conn.close()

    def close(self) -> None:
        return None


class PostgresBackend:
    """Postgres（Neon）后端：共享进程内连接池 + information_schema 自省。

    **不做静默回退**：连不上就抛 ``PostgresUnavailable``（信息里带 postgres/neon）。
    """

    name = "postgres"

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._host = urlsplit(dsn).hostname or ""
        self._pool: _ConnectionPool | None = None  # 懒初始化：构造时不连网

    def connect(self) -> PostgresConnection:
        if self._pool is None:
            self._pool = get_pool(self._dsn)
        return PostgresConnection(self._pool)

    def schema_ddl(self, schema: str) -> str:
        return translate_postgres_schema(schema)

    def table_columns(self, conn: Any, table: str) -> set[str]:
        rows = _execute_raw(
            conn,
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_schema = current_schema() AND table_name = %s",
            (table,),
        ).fetchall()
        return {row["column_name"] for row in rows}

    def table_primary_key(self, conn: Any, table: str) -> tuple[str, ...]:
        rows = _execute_raw(
            conn,
            "SELECT kcu.column_name, kcu.ordinal_position"
            " FROM information_schema.table_constraints tc"
            " JOIN information_schema.key_column_usage kcu"
            "   ON tc.constraint_name = kcu.constraint_name"
            "  AND tc.table_schema = kcu.table_schema"
            "  AND tc.table_name = kcu.table_name"
            " WHERE tc.constraint_type = 'PRIMARY KEY'"
            "   AND tc.table_schema = current_schema()"
            "   AND tc.table_name = %s"
            " ORDER BY kcu.ordinal_position",
            (table,),
        ).fetchall()
        return tuple(row["column_name"] for row in rows)

    def describe(self) -> dict[str, Any]:
        return {"backend": "postgres", "host": self._host, "target": mask_dsn(self._dsn)}

    def ping(self) -> None:
        """一条最轻的连通性验证（不做任何 DDL）。

        理由同 SqliteBackend.ping：健康检查不该跑建表。这份连接是 context manager，
        退出时把连接还给池子（别自己 close，那会绕过池的回收）。
        """

        with self.connect() as conn:
            _execute_raw(conn, "SELECT 1").fetchone()

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()


def resolve_backend(
    db_path: str | Path | None = None, database_url: str | None = None
) -> SqliteBackend | PostgresBackend:
    """选择后端：**显式 db_path 一律 SQLite**；否则看 DATABASE_URL，未设则回退本地 SQLite。

    显式 ``db_path`` 优先是有意为之：测试与 Benchmark 总是传临时库，必须稳定走 SQLite，
    不会因为开发机 ``.env`` 里存在 Neon 连接串就把测试数据写到线上。
    """

    if db_path is not None:
        return SqliteBackend(db_path)
    if database_url is None:
        from app.config import database_url as read_database_url

        database_url = read_database_url()
    if database_url:
        return PostgresBackend(database_url)
    return SqliteBackend(default_db_path())


def describe_configured_backend() -> dict[str, Any]:
    """不连网地描述"当前配置会落到哪个后端"（health 在连库失败时也要如实上报）。"""

    from app.config import database_url as read_database_url

    url = read_database_url()
    if url:
        return PostgresBackend(url).describe()
    return SqliteBackend(default_db_path()).describe()
