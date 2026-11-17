"""端到端演示：策略目录 + 只读关系网关。

运行：python3.11 demo.py
所有写入都在临时内存库中，seed 源库只读。
"""

from policy_catalog import PolicyCatalog
from query_gateway import Gateway, open_seed
from sql_parser import SqlRejectError  # noqa: F401  (调用方可据此识别拒绝)

db = open_seed()
catalog = PolicyCatalog.from_db(db)

# rev1：analyst 拿 bucket(1000)，staff 拿 null；其余列显式 raw。
catalog.publish([
    {"table": "employees", "column": "id", "role": "analyst", "mode": "raw"},
    {"table": "employees", "column": "dept", "role": "analyst", "mode": "raw"},
    {"table": "employees", "column": "salary", "role": "analyst",
     "mode": "bucket", "width": 1000},
    {"table": "budgets", "column": "dept", "role": "analyst", "mode": "raw"},
    {"table": "budgets", "column": "cap", "role": "analyst", "mode": "raw"},
    {"table": "employees", "column": "id", "role": "staff", "mode": "raw"},
    {"table": "employees", "column": "dept", "role": "staff", "mode": "raw"},
    {"table": "employees", "column": "salary", "role": "staff", "mode": "null"},
    {"table": "budgets", "column": "dept", "role": "staff", "mode": "raw"},
    {"table": "budgets", "column": "cap", "role": "staff", "mode": "raw"},
])

gateway = Gateway(db, catalog)
plan = gateway.prepare(
    "SELECT salary, COUNT(*) AS n, SUM(salary) AS s "
    "FROM employees GROUP BY salary ORDER BY salary"
)

for role in ("analyst", "staff"):
    result = gateway.run(plan, [role])
    print(f"--- role={role} revision={result.revision} ---")
    print("columns:", result.columns)
    for row in result.rows:
        print(row)

# 准备期拒绝：拒绝发生在原库执行之前。
for sql in (
    "SELECT * FROM employees; DROP TABLE employees",
    "SELECT * FROM sqlite_master",
    "SELECT AVG(salary) FROM employees",
):
    try:
        gateway.prepare(sql)
    except Exception as exc:  # GatewayError
        print("rejected:", sql, "->", exc)
