"""只读关系网关。

一次 run 的求值边界：
  1. prepare 阶段只做白名单解析/校验 + 空 schema EXPLAIN，不读任何业务数据；
  2. run 阶段绑定一个策略 revision，在全新的隔离内存库中按列计划物化两张
     基表（策略只在这一层应用一次），用户 SQL 全程在隔离库上求值；
  3. 原始库只被受控的固定 SELECT 读取，绝不写入；拒绝均发生在原库执行前。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from policy_catalog import ColumnPlan, CompiledPlan, PolicyCatalog, PolicyError
from sql_parser import SqlRejectError, parse, validate

__all__ = [
    "open_seed",
    "fixed_rows",
    "GatewayError",
    "QueryResult",
    "PreparedQuery",
    "Gateway",
]


def open_seed() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript(Path(__file__).with_name("seed.sql").read_text())
    return db


def fixed_rows(db: sqlite3.Connection):
    return db.execute(
        "select id,dept,salary from employees order by id"
    ).fetchall()


class GatewayError(ValueError):
    """查询被网关拒绝或无法在当前 revision 上执行。"""


def _quote_ident(name: str) -> str:
    return '"' + str(name).replace('"', '""') + '"'


@dataclass(frozen=True)
class QueryResult:
    columns: tuple[str, ...]
    rows: tuple[tuple, ...]
    revision: int
    roles: tuple[str, ...]
    plan_summary: dict

    def to_dict(self) -> dict:
        return {
            "columns": list(self.columns),
            "rows": [list(r) for r in self.rows],
            "revision": self.revision,
            "roles": list(self.roles),
            "plan_summary": self.plan_summary,
        }


class PreparedQuery:
    """已通过白名单与名称校验的不可变查询，可跨角色/run 重用。"""

    def __init__(self, sql: str, program, schema: dict):
        self.sql = sql
        self.program = program
        self.schema = dict(schema)
        # 准备期即在空库上做名称解析，错误早于任何原库数据读取。
        scratch = sqlite3.connect(":memory:")
        try:
            self._create_empty_tables(scratch)
            scratch.execute("EXPLAIN " + sql).fetchall()
        except sqlite3.Error as exc:
            raise GatewayError(f"query rejected: {exc}") from exc
        finally:
            scratch.close()

    def _create_empty_tables(self, db: sqlite3.Connection) -> None:
        for table, columns in self.schema.items():
            cols = ", ".join(f"{_quote_ident(c)} INTEGER" for c in columns)
            db.execute(f"CREATE TABLE {_quote_ident(table)} ({cols})")


class Gateway:
    """绑定一个原始库与一个策略目录的只读网关。"""

    def __init__(self, source: sqlite3.Connection | str | Path, catalog: PolicyCatalog):
        if not isinstance(catalog, PolicyCatalog):
            raise GatewayError("catalog must be a PolicyCatalog instance")
        self._catalog = catalog
        self._owns_source = False
        if isinstance(source, sqlite3.Connection):
            self._source = source
        elif isinstance(source, (str, Path)):
            # 只读 URI：即使代码有缺陷也无法写回原文件。
            uri = Path(source).resolve().as_uri() + "?mode=ro"
            try:
                self._source = sqlite3.connect(uri, uri=True)
            except sqlite3.Error as exc:
                raise GatewayError(f"cannot open source database read-only: {exc}")
            self._owns_source = True
        else:
            raise GatewayError("source must be a sqlite3.Connection or a path")
        self._check_schema()

    # ------------------------------------------------------------------ #
    # 基础设施
    # ------------------------------------------------------------------ #

    @property
    def catalog(self) -> PolicyCatalog:
        return self._catalog

    def close(self) -> None:
        if self._owns_source:
            self._source.close()

    def _check_schema(self) -> None:
        actual: dict[str, list[str]] = {}
        rows = self._source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for (name,) in rows:
            actual[name] = [
                r[1]
                for r in self._source.execute(
                    f'PRAGMA table_info({_quote_ident(name)})'
                ).fetchall()
            ]
        expected = self._catalog.schema
        if set(actual) != set(expected):
            raise GatewayError(
                f"catalog schema tables {sorted(expected)} do not match source "
                f"tables {sorted(actual)}"
            )
        for table, cols in actual.items():
            if tuple(cols) != tuple(expected[table]):
                raise GatewayError(
                    f"column mismatch on {table}: source={cols} "
                    f"catalog={list(expected[table])}"
                )

    # ------------------------------------------------------------------ #
    # prepare / run
    # ------------------------------------------------------------------ #

    def prepare(self, sql: str) -> PreparedQuery:
        try:
            program = parse(sql)
            validate(program, frozenset(self._catalog.schema))
            return PreparedQuery(sql, program, self._catalog.schema)
        except SqlRejectError as exc:
            raise GatewayError(str(exc)) from exc
        except sqlite3.Error as exc:
            raise GatewayError(f"query rejected: {exc}") from exc

    def run(
        self,
        prepared: PreparedQuery,
        roles: Iterable[str],
        revision: int | None = None,
    ) -> QueryResult:
        if not isinstance(prepared, PreparedQuery):
            raise GatewayError("run() requires a PreparedQuery from prepare()")
        try:
            plan = self._catalog.compile(roles, revision)
        except PolicyError as exc:
            raise GatewayError(str(exc)) from exc
        return self._execute(prepared, plan)

    def query(
        self,
        sql: str,
        roles: Iterable[str],
        revision: int | None = None,
    ) -> QueryResult:
        """prepare + run 的便捷入口。"""
        return self.run(self.prepare(sql), roles, revision)

    # ------------------------------------------------------------------ #
    # 隔离求值
    # ------------------------------------------------------------------ #

    def _execute(self, prepared: PreparedQuery, plan: CompiledPlan) -> QueryResult:
        isolated = sqlite3.connect(":memory:")
        try:
            self._materialize(isolated, plan)
            cursor = isolated.execute(prepared.sql)
            columns = tuple(d[0] for d in cursor.description)
            rows = tuple(cursor.fetchall())
        except sqlite3.Error as exc:
            raise GatewayError(f"execution failed on transformed relations: {exc}")
        finally:
            isolated.close()
        return QueryResult(
            columns=columns,
            rows=rows,
            revision=plan.revision,
            roles=plan.roles,
            plan_summary=plan.summary(),
        )

    def _materialize(self, isolated: sqlite3.Connection, plan: CompiledPlan) -> None:
        schema = plan.schema
        for table, columns in schema.items():
            col_def = ", ".join(f"{_quote_ident(c)} INTEGER" for c in columns)
            isolated.execute(
                f"CREATE TABLE {_quote_ident(table)} ({col_def})"
            )
            col_plans = {p.column: p for p in plan.plans_for(table)}
            # 受控的固定取数：全列 SELECT，无用户表达式。
            source_cols = ", ".join(_quote_ident(c) for c in columns)
            raw_rows = self._source.execute(
                f"SELECT {source_cols} FROM {_quote_ident(table)}"
            ).fetchall()
            placeholders = ", ".join("?" for _ in columns)
            converted = [
                tuple(self._convert(col_plans[c], value) for c, value in zip(columns, row))
                for row in raw_rows
            ]
            isolated.executemany(
                f"INSERT INTO {_quote_ident(table)} "
                f"({source_cols}) VALUES ({placeholders})",
                converted,
            )

    @staticmethod
    def _convert(column_plan: ColumnPlan, value):
        if value is None:
            return None
        if type(value) is bool or not isinstance(value, int):
            raise GatewayError(
                f"source value for {column_plan.table}.{column_plan.column} "
                f"is not an int64-compatible integer: {value!r}"
            )
        if not (-(2**63) <= value <= 2**63 - 1):
            raise GatewayError(
                f"source value for {column_plan.table}.{column_plan.column} "
                f"exceeds int64 range: {value!r}"
            )
        return column_plan.apply(value)
