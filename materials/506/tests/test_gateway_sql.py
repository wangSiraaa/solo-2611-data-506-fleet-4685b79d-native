"""SQL 白名单边界、准备期拒绝、CTE 与角色隔离机制。"""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from query_gateway import Gateway, GatewayError, open_seed
from policy_catalog import PolicyCatalog
from sql_parser import SqlRejectError, parse


def build_gateway():
    db = open_seed()
    catalog = PolicyCatalog.from_db(db)
    catalog.publish([
        {"table": "employees", "column": "id", "role": "r", "mode": "raw"},
        {"table": "employees", "column": "dept", "role": "r", "mode": "raw"},
        {"table": "employees", "column": "salary", "role": "r",
         "mode": "bucket", "width": 1000},
        {"table": "budgets", "column": "dept", "role": "r", "mode": "raw"},
        {"table": "budgets", "column": "cap", "role": "r", "mode": "raw"},
    ])
    return db, catalog, Gateway(db, catalog)


ALLOWED = [
    "select id, salary from employees",
    "select * from employees",
    "select e.id from employees e where e.salary > 100 and e.dept = 1",
    "select id from employees where salary is null or dept is not null",
    "select dept, count(*), sum(salary) from employees group by dept order by dept",
    "select salary s, count(*) from employees group by s order by s desc",
    "select salary, count(*) from employees group by 1 order by 2 desc, 1",
    "select e.id from employees e inner join budgets b on e.dept = b.dept",
    "select e.id from employees e left join budgets b on e.dept = b.dept",
    "select e.id from employees e join budgets b on e.dept = b.dept "
    "left join employees m on m.dept = b.dept",
    "with t as (select id, salary from employees) select * from t",
    "with a as (select id from employees) "
    "select a.id from a join budgets b on a.id = b.dept order by a.id",
    "SELECT ID FROM EMPLOYEES WHERE SALARY <> 0",  # 大小写不敏感
]

REJECTED = [
    "select * from employees; drop table employees",
    "insert into employees values(9,1,1)",
    "update employees set salary = 1",
    "delete from employees",
    "pragma table_info(employees)",
    "explain select * from employees",
    "select * from sqlite_master",
    "select 1",  # 无 FROM 字面量查询
    "select distinct id from employees",
    "select avg(salary) from employees",
    "select min(salary), max(salary) from employees",
    "select id + 1 from employees",
    "select count(distinct salary) from employees",
    "select * from employees where dept in (1, 2)",
    "select * from employees where dept between 1 and 2",
    "select * from employees where not salary is null",
    "select * from employees where exists (select 1)",
    "select * from (select id from employees)",
    "with recursive t as (select 1) select * from t",
    "with t as (with u as (select 1) select * from u) select * from t",
    "with employees as (select * from employees) select * from employees",
    "with t as (select id from employees), t as (select id from employees) "
    "select * from t",
    "select * from employees union select * from employees",
    "select * from employees limit 1",
    "select * from employees having count(*) > 1",
    "select * from employees, other_table",
    "select * from unknown_table",
    "select nosuchcolumn from employees",
    "select e.id from employees e cross join budgets b",
    "select e.id from employees e right join budgets b on e.dept = b.dept",
    "select e.id from employees e, budgets b where e.dept = b.dept",
    "select 'x' from employees",  # 字符串字面量
    "select 1.5 from employees",  # 小数字面量
    "attach database ':memory:' as x",
    "select * from employees where salary = (select max(salary) from employees)",
    "create table x(a integer)",
    "",
    ";",
]


class WhitelistTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db, cls.catalog, cls.gateway = build_gateway()

    def test_allowed_queries_prepare(self):
        for sql in ALLOWED:
            with self.subTest(sql=sql):
                self.gateway.prepare(sql)

    def test_rejected_queries(self):
        for sql in REJECTED:
            with self.subTest(sql=sql):
                with self.assertRaises(GatewayError):
                    self.gateway.prepare(sql)

    def test_rejection_happens_before_source_execution(self):
        # 源库在一批拒绝后内容与连接仍可用，且未产生副作用。
        for sql in REJECTED:
            try:
                self.gateway.prepare(sql)
            except GatewayError:
                pass
        rows = self.db.execute("select id from employees order by id").fetchall()
        self.assertEqual(rows, [(1,), (2,), (3,), (4,)])

    def test_parser_directly_rejects(self):
        with self.assertRaises(SqlRejectError):
            parse("select * from t where a = (select 1)")

    def test_cte_is_not_re_masked_as_base_table(self):
        # CTE 里 salary 已按策略转换一次；外层只在该结果上聚合，不再套策略。
        result = self.gateway.query(
            "with t as (select dept d, salary s from employees) "
            "select s, count(*) from t group by s order by s",
            ["r"],
        )
        buckets = [row[0] for row in result.rows]
        self.assertEqual(buckets, [None, 0, 1000, 5000])

    def test_cte_body_cannot_reference_other_ctes(self):
        with self.assertRaises(GatewayError):
            self.gateway.prepare(
                "with a as (select id from employees), "
                "b as (select id from a) select * from b"
            )

    def test_duplicate_output_columns_keep_sql_meaning(self):
        result = self.gateway.query(
            "select id, id, salary from employees order by id", ["r"]
        )
        self.assertEqual(result.columns, ("id", "id", "salary"))
        self.assertEqual(len(result.columns), 3)

    def test_duplicate_rows_preserved(self):
        result = self.gateway.query(
            "select dept from employees order by dept", ["r"]
        )
        self.assertEqual(result.rows, ((1,), (1,), (2,), (2,)))

    def test_ordinal_bounds_checked(self):
        with self.assertRaises(GatewayError):
            self.gateway.prepare(
                "select id from employees group by 2"
            )
        with self.assertRaises(GatewayError):
            self.gateway.prepare(
                "select id from employees order by 3"
            )

    def test_correlation_name_uniqueness(self):
        with self.assertRaises(GatewayError):
            self.gateway.prepare(
                "select a.id from employees a join employees a on a.id = a.id"
            )

    def test_unknown_column_caught_at_prepare_not_run(self):
        prepared = None
        with self.assertRaises(GatewayError):
            prepared = self.gateway.prepare("select ghost from employees")
        self.assertIsNone(prepared)


class ReadOnlyFileTest(unittest.TestCase):
    def test_file_based_source_opened_read_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "seed.db"
            src = sqlite3.connect(path)
            src.executescript(
                (Path(__file__).resolve().parent.parent / "seed.sql").read_text()
            )
            src.commit()
            src.close()
            catalog = PolicyCatalog(
                {"employees": ("id", "dept", "salary"),
                 "budgets": ("dept", "cap")}
            )
            catalog.publish([
                {"table": "employees", "column": "id", "role": "r",
                 "mode": "raw"},
                {"table": "employees", "column": "dept", "role": "r",
                 "mode": "raw"},
                {"table": "employees", "column": "salary", "role": "r",
                 "mode": "null"},
                {"table": "budgets", "column": "dept", "role": "r",
                 "mode": "raw"},
                {"table": "budgets", "column": "cap", "role": "r",
                 "mode": "raw"},
            ])
            gateway = Gateway(path, catalog)
            result = gateway.query(
                "select count(salary) from employees", ["r"]
            )
            self.assertEqual(result.rows, ((0,),))
            gateway.close()
            # 文件内容原样保留。
            check = sqlite3.connect(path)
            self.assertEqual(
                check.execute("select salary from employees where id=1").fetchone(),
                (5000,),
            )
            check.close()

    def test_schema_mismatch_detected(self):
        db = open_seed()
        catalog = PolicyCatalog(
            {"employees": ("id", "dept", "salary", "extra"),
             "budgets": ("dept", "cap")}
        )
        catalog.publish([
            {"table": "employees", "column": "id", "role": "r", "mode": "raw"},
            {"table": "employees", "column": "dept", "role": "r", "mode": "raw"},
            {"table": "employees", "column": "salary", "role": "r",
             "mode": "raw"},
            {"table": "employees", "column": "extra", "role": "r", "mode": "raw"},
            {"table": "budgets", "column": "dept", "role": "r", "mode": "raw"},
            {"table": "budgets", "column": "cap", "role": "r", "mode": "raw"},
        ])
        with self.assertRaises(GatewayError):
            Gateway(db, catalog)


if __name__ == "__main__":
    unittest.main()
