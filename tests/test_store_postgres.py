"""PostgreSQL（Neon）适配层的测试：**只测 SQL 生成与方言函数，不连任何真实数据库**。

为什么不在测试里连 Neon
----------------------
CI 与开发机都不该依赖外网，更不该让测试去 Neon 上建表（会污染线上库）。所以本文件用两个
"最小校验层"替代真实服务器：

- **纯函数层**：直接断言 ``app/db.py`` 的 SQL/DDL 翻译结果（占位符、ON CONFLICT、
  LIKE→ILIKE、类型替换、连接串脱敏、后端选择）。
- **假连接层**：``_FakePromptPool`` / ``_FakePgConnection`` 只在内存里记录执行过的 SQL，
  用来验证 ``PostgresConnection`` 会把 ``execute`` / ``executemany`` / ``executescript``
  都走一遍翻译，并检查生成语句结构合法（括号平衡、无残留 ``?``、冲突目标在插入列内）。

SQLite 回归仍由 ``tests/test_store.py`` 覆盖（默认且行为不变）。

如何用**真实** Postgres/Neon 验证（人工，CI 不跑）
-------------------------------------------------
本机 Docker::

    docker run -d --name tp-pg -e POSTGRES_PASSWORD=tp_pass -e POSTGRES_DB=travelplan \\
        -p 55432:5432 postgres:18-alpine
    DATABASE_URL='postgresql://postgres:tp_pass@localhost:55432/travelplan' \\
        python -c "from app.store import TravelPlanStore; s=TravelPlanStore(); \\
                   s.init_schema(); print(s.describe()); s.close()"

真实 Neon（连接串形如 ``postgresql://...?sslmode=require``，切勿写进仓库）::

    export DATABASE_URL='postgresql://<user>:<pass>@<host>/<db>?sslmode=require'
    python -m pytest -q          # 全量 SQLite 回归（显式 db_path 时忽略 DATABASE_URL）
    python -c "from app.store import TravelPlanStore; s=TravelPlanStore(); s.init_schema(); \\
               print(s.backend_name, s.describe())"   # 应打印 postgres 与脱敏后的 target

已在本机 PostgreSQL 18.6（Docker）上跑过完整的 store 读写与老库迁移（见交付报告）。
"""

from __future__ import annotations

import re

import pytest

from app.db import (
    INSERT_OR_REPLACE_PK,
    DialectError,
    PostgresConnection,
    PostgresUnavailable,
    SqliteBackend,
    close_all_pools,
    describe_configured_backend,
    mask_dsn,
    resolve_backend,
    split_sql_statements,
    to_postgres_sql,
    translate_insert_or_replace,
    translate_postgres_schema,
)
from app.store import DEFAULT_DB_PATH, SCHEMA, TravelPlanStore


# ======================================================================
# 最小校验层：假 Postgres 连接
# ======================================================================


class _FakeCursor:
    def __init__(self, sql: str, params, owner: "_FakePgConnection | None" = None) -> None:
        self._sql = sql
        self._params = params
        self._owner = owner
        self.rowcount = 0

    def executemany(self, sql, seq_of_params):
        rows = list(seq_of_params)
        self.rowcount = len(rows)
        if self._owner is not None:
            self._owner.executed.append((sql, rows))
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakePgConnection:
    """只记录 SQL 的假驱动连接（不连网、不解析 SQL）。"""

    def __init__(self) -> None:
        self.executed: list[tuple[str, object]] = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = False
        self.broken = False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        return _FakeCursor(sql, params, owner=self)

    def cursor(self):
        return _FakeCursor("", None, owner=self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed = True


class _FakePool:
    """假连接池：永远给出同一条假连接，并记录归还。"""

    def __init__(self) -> None:
        self.conn = _FakePgConnection()
        self.released: list[object] = []

    def acquire(self, timeout: float = 0.0):
        return self.conn

    def release(self, conn) -> None:
        self.released.append(conn)


def _assert_structurally_valid_postgres(sql: str) -> None:
    """最小的"Postgres 语法体检"：不需要服务器也能挡掉常见翻译错误。

    先去掉 ``--`` 注释：SCHEMA 的注释里会出现 "INSERT OR REPLACE" 这类字样，
    那不是可执行 SQL。
    """

    sql = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    assert "?" not in sql, f"占位符未翻译: {sql}"
    assert "OR REPLACE" not in sql.upper(), f"INSERT OR REPLACE 未翻译: {sql}"
    assert "OR IGNORE" not in sql.upper(), f"INSERT OR IGNORE 未翻译: {sql}"
    assert "AUTOINCREMENT" not in sql.upper(), f"SQLite 自增语法残留: {sql}"
    assert sql.count("(") == sql.count(")"), f"括号不平衡: {sql}"


# ======================================================================
# 占位符与 INSERT OR REPLACE
# ======================================================================


class TestPlaceholders:
    def test_question_marks_become_percent_s(self):
        sql = "SELECT * FROM runs WHERE run_id=? AND status=?"
        assert to_postgres_sql(sql, bind=True) == (
            "SELECT * FROM runs WHERE run_id=%s AND status=%s"
        )

    def test_no_binding_keeps_sql_readable(self):
        sql = "SELECT DISTINCT provider FROM provider_calls ORDER BY provider"
        assert to_postgres_sql(sql, bind=False) == sql

    def test_literal_percent_is_escaped_only_when_binding(self):
        # psycopg 用 %s 绑定参数，SQL 文本里的字面 % 必须写成 %%。先转义再替换 ? 才不会
        # 把新生成的 %s 也转义掉。
        assert to_postgres_sql("SELECT 1 WHERE x LIKE '%a%' AND y=?", bind=True) == (
            "SELECT 1 WHERE x ILIKE '%%a%%' AND y=%s"
        )
        assert to_postgres_sql("SELECT 1 WHERE x LIKE '%a%'", bind=False) == (
            "SELECT 1 WHERE x ILIKE '%a%'"
        )

    def test_like_becomes_ilike(self):
        sql = "SELECT 1 FROM runs WHERE run_id LIKE ? OR original_query LIKE ?"
        translated = to_postgres_sql(sql, bind=True)
        assert translated.count("ILIKE") == 2
        assert " LIKE " not in translated


class TestInsertOrReplace:
    @pytest.mark.parametrize("table", sorted(INSERT_OR_REPLACE_PK))
    def test_every_registered_table_generates_on_conflict(self, table):
        sql = f"INSERT OR REPLACE INTO {table} (c1, c2) VALUES (?, ?)"
        translated = to_postgres_sql(sql, bind=True)
        _assert_structurally_valid_postgres(translated)
        primary_key = INSERT_OR_REPLACE_PK[table]
        assert translated.startswith(f"INSERT INTO {table} (c1, c2)")
        # 冲突目标 = 该表主键
        assert "ON CONFLICT (" + ", ".join(primary_key) + ")" in translated

    def test_updates_only_non_primary_key_columns(self):
        sql = (
            "INSERT OR REPLACE INTO places (place_id, run_id, name, lat)"
            " VALUES (?, ?, ?, ?)"
        )
        translated = translate_insert_or_replace(sql)
        assert translated.endswith(
            "ON CONFLICT (run_id, place_id) DO UPDATE SET"
            " name=EXCLUDED.name, lat=EXCLUDED.lat"
        )
        # 主键列不出现在 SET 里
        assert "run_id=EXCLUDED.run_id" not in translated
        assert "place_id=EXCLUDED.place_id" not in translated

    def test_insert_or_ignore_becomes_do_nothing(self):
        sql = (
            "INSERT OR IGNORE INTO place_evidence (run_id, place_id, evidence_id)"
            " VALUES (?, ?, ?)"
        )
        assert translate_insert_or_replace(sql) == (
            "INSERT INTO place_evidence (run_id, place_id, evidence_id)"
            " VALUES (?, ?, ?) ON CONFLICT DO NOTHING"
        )

    def test_insert_or_ignore_with_select_gets_clause_at_the_end(self):
        sql = (
            "INSERT OR IGNORE INTO places (run_id, place_id, name)"
            " SELECT run_id, place_id, name FROM places_legacy_v1"
        )
        translated = translate_insert_or_replace(sql)
        assert translated.endswith("FROM places_legacy_v1 ON CONFLICT DO NOTHING")

    def test_unknown_table_raises_dialect_error(self):
        with pytest.raises(DialectError):
            translate_insert_or_replace(
                "INSERT OR REPLACE INTO not_registered (a, b) VALUES (?, ?)"
            )

    def test_statement_without_insert_or_replace_is_untouched(self):
        sql = "INSERT INTO trip_requests (run_id) VALUES (?)"
        assert translate_insert_or_replace(sql) == sql


# ======================================================================
# DDL：一份 schema，两方言
# ======================================================================


class TestSchemaTranslation:
    def test_sqlite_schema_is_returned_verbatim(self, tmp_path):
        backend = SqliteBackend(tmp_path / "t.db")
        assert backend.schema_ddl(SCHEMA) == SCHEMA

    def test_postgres_schema_removes_sqlite_only_syntax(self):
        translated = translate_postgres_schema(SCHEMA)
        _assert_structurally_valid_postgres(translated)
        # 三张自增表（trip_requests / decisions / plan_items）改用 identity 列
        assert translated.upper().count("GENERATED BY DEFAULT AS IDENTITY") == 3
        assert "AUTOINCREMENT" not in translated.upper()
        # REAL 在 Pg 是 4 字节，经纬度/成本必须升到 DOUBLE PRECISION
        assert " REAL" not in translated
        assert "DOUBLE PRECISION" in translated

    def test_both_dialects_define_the_same_tables(self):
        sqlite_tables = set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", SCHEMA))
        pg_tables = set(
            re.findall(
                r"CREATE TABLE IF NOT EXISTS (\w+)", translate_postgres_schema(SCHEMA)
            )
        )
        assert sqlite_tables == pg_tables
        assert len(sqlite_tables) == 21  # 20+ 张表，防止翻译时漏表

    def test_postgres_schema_splits_into_individual_statements(self):
        statements = split_sql_statements(translate_postgres_schema(SCHEMA))
        creates = [s for s in statements if s.upper().startswith("CREATE TABLE")]
        indexes = [s for s in statements if s.upper().startswith("CREATE INDEX")]
        assert len(creates) == 21
        assert len(indexes) >= 10
        for statement in statements:
            _assert_structurally_valid_postgres(statement)

    def test_comment_stripping_does_not_keep_dangling_comment_lines(self):
        statements = split_sql_statements(
            "CREATE TABLE a (x TEXT); -- 说明\n-- 另一行说明\nCREATE INDEX i ON a(x);"
        )
        assert statements == ["CREATE TABLE a (x TEXT)", "CREATE INDEX i ON a(x)"]


# ======================================================================
# 连接串脱敏与后端选择
# ======================================================================


class TestDsnMasking:
    def test_credentials_are_masked_but_host_kept(self):
        masked = mask_dsn(
            "postgresql://alice:s3cr3t-password@ep-cool-123.us-east-2.aws.neon.tech/neondb"
            "?sslmode=require"
        )
        assert masked == "postgresql://***@ep-cool-123.us-east-2.aws.neon.tech/neondb"
        assert "alice" not in masked
        assert "s3cr3t-password" not in masked

    def test_describe_for_postgres_never_leaks_secret(self):
        backend = resolve_backend(
            None, "postgresql://bob:hunter2@db.example.com:5432/travelplan?sslmode=require"
        )
        described = backend.describe()
        assert described["backend"] == "postgres"
        assert described["host"] == "db.example.com"
        assert "bob" not in str(described)
        assert "hunter2" not in str(described)


class TestBackendSelection:
    def test_explicit_db_path_always_wins_over_database_url(self, tmp_path):
        # 这是"测试绝不写到 Neon"的硬保证：显式 db_path 时 DATABASE_URL 被忽略。
        store = TravelPlanStore(
            db_path=tmp_path / "x.db", database_url="postgresql://u:p@127.0.0.1:1/db"
        )
        assert store.backend_name == "sqlite"
        assert store.db_path == tmp_path / "x.db"
        assert store.describe()["backend"] == "sqlite"

    def test_no_database_url_falls_back_to_local_sqlite(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("TRAVELPLAN_DB_PATH", str(tmp_path / "local.db"))
        store = TravelPlanStore()
        assert store.backend_name == "sqlite"
        assert store.db_path is not None and store.db_path.name == "local.db"

    def test_database_url_selects_postgres_without_connecting(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@example.invalid/db")
        backend = resolve_backend(None, None)
        assert backend.name == "postgres"
        # describe() 只需读取连接串文本，不会发起任何连接。
        assert backend.describe()["host"] == "example.invalid"
        close_all_pools()

    def test_describe_configured_backend_reports_postgres_without_connecting(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            "DATABASE_URL", "postgresql://u:secret@example.invalid/travelplan"
        )
        described = describe_configured_backend()
        assert described == {
            "backend": "postgres",
            "host": "example.invalid",
            "target": "postgresql://***@example.invalid/travelplan",
        }
        assert "secret" not in str(described)

    def test_describe_configured_backend_defaults_to_sqlite(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.setenv("TRAVELPLAN_DB_PATH", str(tmp_path / "default.db"))
        described = describe_configured_backend()
        assert described["backend"] == "sqlite"
        assert described["path"].endswith("default.db")
        assert DEFAULT_DB_PATH.name == "travelplan.db"


# ======================================================================
# 假连接层：验证翻译真的发生在连接包装里
# ======================================================================


class TestPostgresConnectionWrapper:
    def test_execute_translates_and_commits_on_success(self):
        pool = _FakePool()
        with PostgresConnection(pool) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO runs (run_id, user_id) VALUES (?, ?)",
                ("r1", "u1"),
            )
        sql, params = pool.conn.executed[-1]
        assert sql == (
            "INSERT INTO runs (run_id, user_id) VALUES (%s, %s)"
            " ON CONFLICT (run_id) DO UPDATE SET user_id=EXCLUDED.user_id"
        )
        assert params == ("r1", "u1")
        assert pool.conn.committed == 1
        assert pool.conn.rolled_back == 0
        assert pool.released  # 连接被归还

    def test_rolls_back_and_releases_on_error(self):
        pool = _FakePool()
        with pytest.raises(ValueError):
            with PostgresConnection(pool) as conn:
                conn.execute("SELECT ?", (1,))
                raise ValueError("boom")
        assert pool.conn.rolled_back == 1
        assert pool.conn.committed == 0
        assert pool.released

    def test_executemany_translates_all_rows(self):
        pool = _FakePool()
        with PostgresConnection(pool) as conn:
            conn.executemany(
                "INSERT INTO decisions (run_id, decision) VALUES (?, ?)",
                [("r1", "KEEP"), ("r1", "REJECT")],
            )
        sql, rows = pool.conn.executed[-1]
        assert sql == "INSERT INTO decisions (run_id, decision) VALUES (%s, %s)"
        assert rows == [("r1", "KEEP"), ("r1", "REJECT")]

    def test_executescript_splits_translated_ddl(self):
        pool = _FakePool()
        with PostgresConnection(pool) as conn:
            conn.executescript(translate_postgres_schema(SCHEMA))
        statements = [sql for sql, _ in pool.conn.executed]
        assert len(statements) == 35  # 21 张表 + 14 个索引
        for sql in statements:
            _assert_structurally_valid_postgres(sql)

    def test_raw_execute_does_not_double_escape(self):
        # 方言自省 SQL 已经是 Pg 写法，再走一遍翻译会把 %s 变成 %%s。
        pool = _FakePool()
        with PostgresConnection(pool) as conn:
            conn.raw_execute(
                "SELECT column_name FROM information_schema.columns"
                " WHERE table_name = %s",
                ("runs",),
            )
        sql, params = pool.conn.executed[-1]
        assert sql.count("%s") == 1
        assert "%%s" not in sql
        assert params == ("runs",)


# ======================================================================
# "绝不静默回退"：连不上就抛清晰异常
# ======================================================================


class TestNoSilentFallback:
    def test_unreachable_postgres_raises_clear_error(self):
        pytest.importorskip("psycopg")
        # 端口 1 一定连不上；connect_timeout 让失败在秒级返回。
        backend = resolve_backend(
            None, "postgresql://u:p@127.0.0.1:1/nope?connect_timeout=2"
        )
        assert backend.name == "postgres"
        try:
            with pytest.raises(PostgresUnavailable) as info:
                backend.connect()
            message = str(info.value).lower()
            assert "postgres" in message or "neon" in message
        finally:
            close_all_pools()

    def test_store_creation_fails_loudly_instead_of_writing_sqlite(
        self, monkeypatch, tmp_path
    ):
        pytest.importorskip("psycopg")
        # 万一发生静默回退，SQLite 库会落在这个路径上；断言它**不存在**。
        target = tmp_path / "should_not_be_created.db"
        monkeypatch.setenv("TRAVELPLAN_DB_PATH", str(target))
        try:
            with pytest.raises(PostgresUnavailable):
                TravelPlanStore(
                    database_url="postgresql://u:p@127.0.0.1:1/nope?connect_timeout=2"
                )
            assert not target.exists()
        finally:
            close_all_pools()
