"""独立手工转换的小库交叉核对。

reference_db() 不经过 PolicyCatalog / 网关任何代码：按显式给出的每列模式
手工转换 seed 数据，写入另一个全新 SQLite 连接，再执行同一条 SQL，结果必须
与网关逐行逐列一致。
"""

import sqlite3
import unittest
from pathlib import Path

from query_gateway import Gateway, GatewayError, open_seed
from policy_catalog import PolicyCatalog

SEED = Path(__file__).resolve().parent.parent / "seed.sql"


def hand_convert(value, mode, width=None):
    """与被测实现完全独立书写的转换（显式分支，不调用 mask/ColumnPlan）。"""
    if value is None:
        return None
    if mode == "null":
        return None
    if mode == "raw":
        return value
    if mode == "bucket":
        assert isinstance(width, int) and width > 0
        # 向下取整边界：floor(value / width) * width
        return (value // width) * width
    raise AssertionError(mode)


def reference_db(employees_spec, budgets_spec):
    """在独立连接里按手工映射重建两张转换后的表。"""
    ref = sqlite3.connect(":memory:")
    ref.execute("CREATE TABLE employees(id INTEGER, dept INTEGER, salary INTEGER)")
    ref.execute("CREATE TABLE budgets(dept INTEGER, cap INTEGER)")
    src = sqlite3.connect(":memory:")
    src.executescript(SEED.read_text())
    try:
        emp_rows = src.execute("SELECT id, dept, salary FROM employees").fetchall()
        bud_rows = src.execute("SELECT dept, cap FROM budgets").fetchall()
    finally:
        src.close()
    for row in emp_rows:
        ref.execute(
            "INSERT INTO employees VALUES (?, ?, ?)",
            tuple(
                hand_convert(v, *employees_spec[col])
                for col, v in zip(("id", "dept", "salary"), row)
            ),
        )
    for row in bud_rows:
        ref.execute(
            "INSERT INTO budgets VALUES (?, ?)",
            tuple(
                hand_convert(v, *budgets_spec[col])
                for col, v in zip(("dept", "cap"), row)
            ),
        )
    return ref


# 角色与策略 ---------------------------------------------------------------- #

# null_only：没有任何匹配策略 => 全部列按 null（含 id、dept、cap）。
# analyst_bucket：显式 bucket/raw 组合。
# auditor：全 raw。
POLICIES = [
    {"table": "employees", "column": "salary", "role": "analyst",
     "mode": "bucket", "width": 1000},
    {"table": "employees", "column": "id", "role": "analyst", "mode": "raw"},
    {"table": "employees", "column": "dept", "role": "analyst", "mode": "raw"},
    {"table": "budgets", "column": "dept", "role": "analyst", "mode": "raw"},
    {"table": "budgets", "column": "cap", "role": "analyst",
     "mode": "bucket", "width": 500},

    {"table": "employees", "column": "salary", "role": "auditor", "mode": "raw"},
    {"table": "employees", "column": "id", "role": "auditor", "mode": "raw"},
    {"table": "employees", "column": "dept", "role": "auditor", "mode": "raw"},
    {"table": "budgets", "column": "dept", "role": "auditor", "mode": "raw"},
    {"table": "budgets", "column": "cap", "role": "auditor", "mode": "raw"},
]

ANALYST_SPEC_EMP = {
    "id": ("raw",), "dept": ("raw",), "salary": ("bucket", 1000),
}
ANALYST_SPEC_BUD = {"dept": ("raw",), "cap": ("bucket", 500)}
NULL_SPEC_EMP = {
    "id": ("null",), "dept": ("null",), "salary": ("null",),
}
NULL_SPEC_BUD = {"dept": ("null",), "cap": ("null",)}
RAW_SPEC_EMP = {
    "id": ("raw",), "dept": ("raw",), "salary": ("raw",),
}
RAW_SPEC_BUD = {"dept": ("raw",), "cap": ("raw",)}

QUERIES = [
    "SELECT id, salary FROM employees WHERE salary = 5000 ORDER BY id",
    "SELECT id FROM employees WHERE salary IS NULL ORDER BY id",
    "SELECT id FROM employees WHERE salary IS NOT NULL ORDER BY id",
    "SELECT COUNT(salary), SUM(salary), COUNT(*) FROM employees",
    "SELECT salary, COUNT(*), SUM(salary) FROM employees "
    "GROUP BY salary ORDER BY salary",
    # 950 -> 桶 0，1000 -> 桶 1000：必须分在不同桶。
    "SELECT dept, salary FROM employees ORDER BY id",
    "SELECT e.id, e.salary, b.cap FROM employees e "
    "INNER JOIN budgets b ON e.dept = b.dept ORDER BY e.id",
    "SELECT e.id, b.cap FROM employees e "
    "LEFT JOIN budgets b ON e.dept = b.dept AND e.salary = b.cap ORDER BY e.id",
    "SELECT e.dept, SUM(e.salary), COUNT(e.id) FROM employees e "
    "JOIN budgets b ON e.dept = b.dept "
    "WHERE e.salary >= 1000 GROUP BY e.dept ORDER BY e.dept",
    "WITH t AS (SELECT dept d, salary s FROM employees) "
    "SELECT d, s, COUNT(*) FROM t GROUP BY d, s ORDER BY d, s",
    "WITH x AS (SELECT e.id i, b.cap c FROM employees e "
    "JOIN budgets b ON e.dept = b.dept) "
    "SELECT i, c FROM x WHERE c IS NOT NULL ORDER BY i",
    # OR / AND / 比较混合（无括号谓词，括号不在白名单内）。
    "SELECT id FROM employees WHERE salary = 0 OR dept = 2 AND salary = 1000 "
    "ORDER BY id",
]


class CrossCheckTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = open_seed()
        cls.catalog = PolicyCatalog.from_db(cls.source)
        cls.rev1 = cls.catalog.publish(POLICIES)
        cls.gateway = Gateway(cls.source, cls.catalog)

    def _assert_same_as_reference(self, sql, roles, emp_spec, bud_spec, revision=None):
        prepared = self.gateway.prepare(sql)
        result = self.gateway.run(prepared, roles, revision)
        ref = reference_db(emp_spec, bud_spec)
        try:
            cur = ref.execute(sql)
            ref_columns = tuple(d[0] for d in cur.description)
            ref_rows = tuple(cur.fetchall())
        finally:
            ref.close()
        self.assertEqual(result.columns, ref_columns, sql)
        self.assertEqual(result.rows, ref_rows, sql)
        return result

    def test_null_only_identity_cannot_see_original_values(self):
        # WHERE salary=5000 找不到原值为 5000 的人；COUNT(salary)=0。
        result = self._assert_same_as_reference(
            "SELECT id, salary FROM employees WHERE salary = 5000 ORDER BY id",
            ["null_only"], NULL_SPEC_EMP, NULL_SPEC_BUD,
        )
        self.assertEqual(result.rows, ())
        count = self.gateway.query(
            "SELECT COUNT(salary) FROM employees", ["null_only"]
        )
        self.assertEqual(count.rows, ((0,),))
        # 无匹配策略 => 每列计划都是 null 且 matched=False。
        self.assertTrue(
            all(c["mode"] == "null" and c["matched"] is False
                for c in count.plan_summary["columns"])
        )

    def test_bucket_splits_950_and_1000(self):
        result = self.gateway.query(
            "SELECT salary, COUNT(*) FROM employees GROUP BY salary ORDER BY salary",
            ["analyst"],
        )
        buckets = {row[0]: row[1] for row in result.rows}
        self.assertIn(0, buckets)   # 950 落入 0 桶
        self.assertIn(1000, buckets)  # 1000 落入 1000 桶，两者不同桶
        self.assertEqual(buckets[None], 1)  # NULL 保持 NULL 自成一组

    def test_all_queries_match_hand_built_database(self):
        cases = [
            (["null_only"], NULL_SPEC_EMP, NULL_SPEC_BUD),
            (["analyst"], ANALYST_SPEC_EMP, ANALYST_SPEC_BUD),
            (["auditor"], RAW_SPEC_EMP, RAW_SPEC_BUD),
        ]
        for roles, emp_spec, bud_spec in cases:
            for sql in QUERIES:
                with self.subTest(roles=roles, sql=sql):
                    self._assert_same_as_reference(
                        sql, roles, emp_spec, bud_spec
                    )

    def test_revision_pin_and_publish_during_run(self):
        # 发布 rev2：analyst 的 salary 改为 null 策略。
        rev2 = self.catalog.publish(POLICIES + [
            {"table": "employees", "column": "salary", "role": "analyst",
             "mode": "null"},
        ])
        self.assertEqual(rev2, 2)
        prepared = self.gateway.prepare(
            "SELECT salary, COUNT(*) FROM employees GROUP BY salary ORDER BY salary"
        )
        # 显式绑定 rev1：结果停留在 bucket 世界。
        pinned = self.gateway.run(prepared, ["analyst"], revision=self.rev1)
        ref_old = reference_db(ANALYST_SPEC_EMP, ANALYST_SPEC_BUD)
        try:
            self.assertEqual(
                pinned.rows,
                tuple(ref_old.execute(prepared.sql).fetchall()),
            )
        finally:
            ref_old.close()
        self.assertEqual(pinned.revision, 1)
        # 未绑定版本的新请求采用 rev2（null 强于 bucket）。
        latest = self.gateway.run(prepared, ["analyst"])
        self.assertEqual(latest.revision, 2)
        self.assertEqual(latest.rows, ((None, 4),))
        # 同一 prepared plan 再绑 rev1 仍然可用且不被 rev2 污染。
        again = self.gateway.run(prepared, ["analyst"], revision=1)
        self.assertEqual(again.revision, 1)
        self.assertNotEqual(again.rows, latest.rows)

    def test_interleaved_plan_reuse_between_two_roles(self):
        prepared = self.gateway.prepare(
            "SELECT id, salary FROM employees ORDER BY id"
        )
        ref_analyst = reference_db(ANALYST_SPEC_EMP, ANALYST_SPEC_BUD)
        ref_null = reference_db(NULL_SPEC_EMP, NULL_SPEC_BUD)
        try:
            expected_analyst = tuple(ref_analyst.execute(prepared.sql).fetchall())
            expected_null = tuple(ref_null.execute(prepared.sql).fetchall())
        finally:
            ref_analyst.close()
            ref_null.close()
        sequence = []
        for i in range(4):  # 交错复用同一 plan，角色不能串。
            sequence.append(("analyst", self.gateway.run(prepared, ["analyst"])))
            sequence.append(("null_only", self.gateway.run(prepared, ["null_only"])))
        for role, result in sequence:
            if role == "analyst":
                self.assertEqual(result.rows, expected_analyst)
                self.assertEqual(result.roles, ("analyst",))
            else:
                self.assertEqual(result.rows, expected_null)
                self.assertEqual(result.roles, ("null_only",))

    def test_source_database_is_never_modified(self):
        before = self.source.execute(
            "SELECT id, dept, salary FROM employees ORDER BY id"
        ).fetchall()
        self.gateway.query(
            "SELECT e.id, e.salary, b.cap FROM employees e "
            "JOIN budgets b ON e.dept = b.dept",
            ["analyst"],
        )
        after = self.source.execute(
            "SELECT id, dept, salary FROM employees ORDER BY id"
        ).fetchall()
        self.assertEqual(before, after)
        self.assertEqual(
            self.source.execute("SELECT COUNT(*) FROM budgets").fetchone(), (2,)
        )

    def test_every_result_carries_revision_and_summary(self):
        result = self.gateway.query("SELECT * FROM employees", ["analyst"])
        self.assertEqual(result.revision, 1)
        summary = result.plan_summary
        self.assertEqual(summary["revision"], 1)
        self.assertEqual(summary["roles"], ["analyst"])
        modes = {(c["table"], c["column"]): (c["mode"], c["width"])
                 for c in summary["columns"]}
        self.assertEqual(modes[("employees", "salary")], ("bucket", 1000))
        self.assertEqual(modes[("budgets", "cap")], ("bucket", 500))
        self.assertEqual(modes[("employees", "id")], ("raw", None))


if __name__ == "__main__":
    unittest.main()
