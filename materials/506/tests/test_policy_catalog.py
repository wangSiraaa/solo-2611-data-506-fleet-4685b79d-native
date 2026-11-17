"""PolicyCatalog：发布校验、revision 保留、组合规则与可独立检查的计划。"""

import unittest

from query_gateway import open_seed
from policy_catalog import (
    ColumnPlan,
    CompiledPlan,
    PolicyCatalog,
    PolicyError,
    mask,
)

SCHEMA = {"employees": ("id", "dept", "salary"), "budgets": ("dept", "cap")}


class ScalarMaskTest(unittest.TestCase):
    def test_scalar_mask_legacy(self):
        self.assertIsNone(mask(None, "raw"))
        self.assertIsNone(mask(5, "null"))
        self.assertEqual(mask(950, "bucket", 1000), 0)
        self.assertEqual(mask(1000, "bucket", 1000), 1000)
        self.assertEqual(mask(-7, "bucket", 10), -10)
        self.assertEqual(mask(5, "raw"), 5)
        with self.assertRaises(ValueError):
            mask(5, "bucket", 0)


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.cat = PolicyCatalog(SCHEMA)

    def test_publish_all_or_nothing_and_revisions(self):
        good = [
            {"table": "employees", "column": "salary", "role": "a", "mode": "null"},
        ]
        self.assertEqual(self.cat.publish(good), 1)
        bad = good + [
            {"table": "employees", "column": "salary", "role": "b",
             "mode": "bucket", "width": True},
        ]
        with self.assertRaises(PolicyError):
            self.cat.publish(bad)
        self.assertEqual(self.cat.revisions(), (1,))  # 失败不产生 revision

        r2 = [
            {"table": "employees", "column": "salary", "role": "a", "mode": "raw"},
        ]
        self.assertEqual(self.cat.publish(r2), 2)
        self.assertEqual(self.cat.revisions(), (1, 2))
        self.assertEqual(self.cat.latest_revision, 2)
        # 旧 revision 仍可查且内容不变。
        old = self.cat.get_revision_policies(1)
        self.assertEqual(old[0].mode, "null")

    def test_unknown_table_and_column_rejected(self):
        with self.assertRaises(PolicyError):
            self.cat.publish(
                [{"table": "nope", "column": "id", "role": "a", "mode": "raw"}]
            )
        with self.assertRaises(PolicyError):
            self.cat.publish(
                [{"table": "employees", "column": "nope", "role": "a",
                  "mode": "raw"}]
            )

    def test_mode_and_width_validation(self):
        base = {"table": "employees", "column": "salary", "role": "a"}
        for width in (0, -1, 1.0, "10", True, False):
            with self.assertRaises(PolicyError):
                self.cat.publish([{**base, "mode": "bucket", "width": width}])
        with self.assertRaises(PolicyError):
            self.cat.publish([{**base, "mode": "bucket"}])  # 缺 width
        with self.assertRaises(PolicyError):
            self.cat.publish([{**base, "mode": "raw", "width": 10}])
        with self.assertRaises(PolicyError):
            self.cat.publish([{**base, "mode": "weird"}])

    def test_duplicate_identity_and_extra_keys(self):
        p = {"table": "employees", "column": "salary", "role": "a", "mode": "null"}
        with self.assertRaises(PolicyError):
            self.cat.publish([p, dict(p)])
        with self.assertRaises(PolicyError):
            self.cat.publish([{**p, "expression": "salary/1000"}])

    def test_schema_binding_from_db(self):
        cat = PolicyCatalog.from_db(open_seed())
        self.assertEqual(cat.schema, dict(SCHEMA))
        with self.assertRaises(PolicyError):
            PolicyCatalog({})
        with self.assertRaises(PolicyError):
            PolicyCatalog({"t": ()})


class CompileTest(unittest.TestCase):
    def setUp(self):
        self.cat = PolicyCatalog(SCHEMA)
        self.cat.publish([
            {"table": "employees", "column": "salary", "role": "null_role",
             "mode": "null"},
            {"table": "employees", "column": "salary", "role": "b1000",
             "mode": "bucket", "width": 1000},
            {"table": "employees", "column": "salary", "role": "b500",
             "mode": "bucket", "width": 500},
            {"table": "employees", "column": "salary", "role": "raw_role",
             "mode": "raw"},
            {"table": "employees", "column": "id", "role": "raw_role", "mode": "raw"},
        ])

    def test_no_match_defaults_null(self):
        plan = self.cat.compile(["nobody"])
        col = plan.get("employees", "salary")
        self.assertEqual((col.mode, col.width, col.matched), ("null", None, False))

    def test_combine_null_strongest(self):
        plan = self.cat.compile(["null_role", "b1000", "raw_role"])
        col = plan.get("employees", "salary")
        self.assertEqual((col.mode, col.width), ("null", None))

    def test_combine_bucket_beats_raw_and_max_width(self):
        plan = self.cat.compile(["b1000", "b500", "raw_role"])
        col = plan.get("employees", "salary")
        self.assertEqual((col.mode, col.width), ("bucket", 1000))

    def test_raw_combination(self):
        plan = self.cat.compile(["raw_role"])
        self.assertEqual(plan.get("employees", "id").mode, "raw")
        self.assertEqual(plan.get("employees", "salary").mode, "raw")

    def test_plan_is_independent_and_versioned(self):
        plan = self.cat.compile(["b500"])
        self.assertIsInstance(plan, CompiledPlan)
        self.assertEqual(plan.revision, 1)
        self.assertEqual(plan.roles, ("b500",))
        self.assertEqual(plan.schema, dict(SCHEMA))
        all_tables = {(p.table, p.column) for p in plan.columns}
        self.assertEqual(
            all_tables,
            {("employees", "id"), ("employees", "dept"), ("employees", "salary"),
             ("budgets", "dept"), ("budgets", "cap")},
        )
        summary = plan.summary()
        self.assertEqual(summary["revision"], 1)
        self.assertEqual(summary["roles"], ["b500"])
        # 应用函数：NULL 保持 NULL，负值按 floor 语义。
        col = plan.get("employees", "salary")
        self.assertIsNone(col.apply(None))
        self.assertEqual(col.apply(950), 500)
        self.assertEqual(col.apply(-950), -1000)

    def test_compile_unknown_revision_and_bad_roles(self):
        with self.assertRaises(PolicyError):
            self.cat.compile(["b500"], revision=99)
        with self.assertRaises(PolicyError):
            self.cat.compile([1])
        with self.assertRaises(PolicyError):
            self.cat.compile([""])
        empty = PolicyCatalog(SCHEMA)
        with self.assertRaises(PolicyError):
            empty.compile(["x"])  # 无任何 revision

    def test_old_revision_still_compilable(self):
        self.cat.publish([
            {"table": "employees", "column": "salary", "role": "raw_role",
             "mode": "raw"},
        ])
        old = self.cat.compile(["raw_role"], revision=1)
        new = self.cat.compile(["raw_role"], revision=2)
        self.assertEqual(old.get("employees", "salary").mode, "raw")
        self.assertEqual(old.revision, 1)
        self.assertEqual(new.revision, 2)


if __name__ == "__main__":
    unittest.main()
