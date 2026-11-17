"""列级掩码策略目录。

策略只声明 (表, 列, 角色, mode[, width])，不允许引用其他列或任何可执行
代码。发布是全有或全无的：只有全部策略通过真实表/列绑定、模式参数与重复
身份检查后，才产生一个新的不可变 revision；旧 revision 永久保留。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

__all__ = [
    "mask",
    "PolicyError",
    "Policy",
    "ColumnPlan",
    "CompiledPlan",
    "PolicyCatalog",
]

MODES = ("raw", "null", "bucket")


def mask(value, mode, width=None):
    """起点材料保留的标量掩码函数。"""
    if value is None or mode == "null":
        return None
    if mode == "raw":
        return value
    if mode == "bucket" and type(width) is int and width > 0:
        return (value // width) * width
    raise ValueError("invalid mask")


class PolicyError(ValueError):
    """策略声明、revision 或角色参数非法。"""


@dataclass(frozen=True)
class Policy:
    table: str
    column: str
    role: str
    mode: str
    width: int | None = None

    @property
    def identity(self):
        return (self.table, self.column, self.role, self.mode, self.width)


@dataclass(frozen=True)
class ColumnPlan:
    """单个绑定列对一组角色的最终变换计划。"""

    table: str
    column: str
    mode: str  # 'raw' | 'null' | 'bucket'
    width: int | None
    matched: bool  # 是否存在至少一条匹配策略（无匹配按 null）
    revision: int

    def apply(self, value):
        if value is None:
            return None
        if self.mode == "null":
            return None
        if self.mode == "raw":
            return value
        return (value // self.width) * self.width

    def summary(self) -> dict:
        return {
            "table": self.table,
            "column": self.column,
            "mode": self.mode,
            "width": self.width,
            "matched": self.matched,
        }


@dataclass(frozen=True)
class CompiledPlan:
    """compile() 的结果，可脱离目录独立检查。"""

    revision: int
    roles: tuple[str, ...]
    schema: Mapping[str, tuple[str, ...]]
    columns: tuple[ColumnPlan, ...]

    def get(self, table: str, column: str) -> ColumnPlan:
        for plan in self.columns:
            if plan.table == table and plan.column == column:
                return plan
        raise PolicyError(f"unbound column {table}.{column}")

    def plans_for(self, table: str) -> tuple[ColumnPlan, ...]:
        return tuple(p for p in self.columns if p.table == table)

    def summary(self) -> dict:
        return {
            "revision": self.revision,
            "roles": list(self.roles),
            "columns": [p.summary() for p in self.columns],
        }


_ALLOWED_KEYS = frozenset({"table", "column", "role", "mode", "width"})


class PolicyCatalog:
    """持有真实 schema 与各 revision 的不可变策略集合。"""

    def __init__(self, schema: Mapping[str, Sequence[str]]):
        bound: dict[str, tuple[str, ...]] = {}
        if not isinstance(schema, Mapping) or not schema:
            raise PolicyError("schema must be a non-empty mapping of table -> columns")
        for table, columns in schema.items():
            self._check_name(table, "table")
            cols = tuple(columns)
            if not cols or any(not isinstance(c, str) or not c for c in cols):
                raise PolicyError(f"invalid columns for table {table!r}")
            if len(set(cols)) != len(cols):
                raise PolicyError(f"duplicate columns in schema for {table!r}")
            bound[table] = cols
        self._schema: dict[str, tuple[str, ...]] = bound
        self._revisions: list[tuple[Policy, ...]] = []

    @classmethod
    def from_db(cls, db: sqlite3.Connection) -> "PolicyCatalog":
        """从真实 SQLite 库绑定表与列（只读 sqlite_master 查询）。"""
        schema: dict[str, list[str]] = {}
        rows = db.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        ).fetchall()
        for name, _sql in rows:
            cols = [r[1] for r in db.execute(f'PRAGMA table_info("{name}")').fetchall()]
            schema[name] = cols
        return cls(schema)

    @staticmethod
    def _check_name(value, what: str) -> None:
        if not isinstance(value, str) or not value:
            raise PolicyError(f"{what} must be a non-empty string")

    @property
    def schema(self) -> Mapping[str, tuple[str, ...]]:
        return dict(self._schema)

    @property
    def latest_revision(self) -> int:
        if not self._revisions:
            raise PolicyError("no policy revision has been published")
        return len(self._revisions)

    def revisions(self) -> tuple[int, ...]:
        return tuple(range(1, len(self._revisions) + 1))

    def get_revision_policies(self, revision: int) -> tuple[Policy, ...]:
        self._check_revision(revision)
        return self._revisions[revision - 1]

    def _check_revision(self, revision: int) -> None:
        if type(revision) is not int or not (1 <= revision <= len(self._revisions)):
            raise PolicyError(f"unknown policy revision {revision!r}")

    def publish(self, policies: Iterable[Mapping]) -> int:
        """校验并发布一批策略，成功才分配新 revision，返回 revision 号。"""
        if not isinstance(policies, Iterable) or isinstance(policies, (str, bytes)):
            raise PolicyError("policies must be an iterable of policy mappings")
        validated: list[Policy] = []
        seen: set[tuple] = set()
        for index, raw in enumerate(policies):
            policy = self._validate_policy(raw, index)
            if policy.identity in seen:
                raise PolicyError(
                    f"duplicate policy identity at index {index}: {policy.identity}"
                )
            seen.add(policy.identity)
            validated.append(policy)
        # 全部有效后才落地，失败不影响既有 revision。
        self._revisions.append(tuple(validated))
        return len(self._revisions)

    def _validate_policy(self, raw: Mapping, index: int) -> Policy:
        where = f"policy at index {index}"
        if not isinstance(raw, Mapping):
            raise PolicyError(f"{where} must be a mapping")
        extra = set(raw) - _ALLOWED_KEYS
        if extra:
            raise PolicyError(f"{where} has unsupported keys: {sorted(extra)}")
        try:
            table = raw["table"]
            column = raw["column"]
            role = raw["role"]
            mode = raw["mode"]
        except KeyError as exc:
            raise PolicyError(f"{where} missing key {exc.args[0]!r}") from None
        self._check_name(table, f"{where}.table")
        self._check_name(role, f"{where}.role")
        if table not in self._schema:
            raise PolicyError(f"{where} references unknown table {table!r}")
        if not isinstance(column, str) or not column:
            raise PolicyError(f"{where}.column must be a non-empty string")
        if column not in self._schema[table]:
            raise PolicyError(
                f"{where} references unknown column {table}.{column}"
            )
        if mode not in MODES:
            raise PolicyError(
                f"{where} mode must be one of {MODES}, got {mode!r}"
            )
        width = raw.get("width")
        if mode == "bucket":
            # bool 是 int 的子类，必须显式拒绝；浮点、零、负数同样拒绝。
            if type(width) is not int or width <= 0:
                raise PolicyError(
                    f"{where} bucket width must be a positive int, got {width!r}"
                )
        elif width is not None:
            raise PolicyError(f"{where} width is only valid with mode='bucket'")
        return Policy(table=table, column=column, role=role, mode=mode, width=width)

    def compile(
        self,
        roles: Iterable[str],
        revision: int | None = None,
    ) -> CompiledPlan:
        """把指定 revision 的策略组合成每列一个变换计划。"""
        if isinstance(roles, (str, bytes)) or not isinstance(roles, Iterable):
            raise PolicyError("roles must be an iterable of role strings")
        role_tuple = tuple(roles)
        if any(not isinstance(r, str) or not r for r in role_tuple):
            raise PolicyError("every role must be a non-empty string")
        role_set = frozenset(role_tuple)
        if revision is None:
            revision = self.latest_revision
        self._check_revision(revision)
        policies = self._revisions[revision - 1]

        plans: list[ColumnPlan] = []
        for table, columns in self._schema.items():
            for column in columns:
                matched = [
                    p
                    for p in policies
                    if p.table == table and p.column == column and p.role in role_set
                ]
                mode, width, has_match = self._combine(matched)
                plans.append(
                    ColumnPlan(
                        table=table,
                        column=column,
                        mode=mode,
                        width=width,
                        matched=has_match,
                        revision=revision,
                    )
                )
        return CompiledPlan(
            revision=revision,
            roles=role_tuple,
            schema=self.schema,
            columns=tuple(plans),
        )

    @staticmethod
    def _combine(matched: Sequence[Policy]) -> tuple[str, int | None, bool]:
        """组合规则：null 强于 bucket 强于 raw；多个 bucket 取最大 width。"""
        if not matched:
            return "null", None, False
        modes = {p.mode for p in matched}
        if "null" in modes:
            return "null", None, True
        buckets = [p for p in matched if p.mode == "bucket"]
        if buckets:
            return "bucket", max(p.width for p in buckets), True
        return "raw", None, True
