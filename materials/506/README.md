# 起点与运行

运行：`python3.11 demo.py`（仅标准库，无第三方依赖）。所有写入都在每次 run 的
隔离内存库中，seed 源库保持只读；测试：`python3.11 -m unittest discover -s tests`。

## 模块

- `policy_catalog.py`
  - 保留起点标量函数 `mask(value, mode, width=None)`。
  - `PolicyCatalog(schema)` / `PolicyCatalog.from_db(db)`：绑定真实表与列。
  - `publish(policies)`：全有或全无发布，成功才产生新 revision；校验真实表/列、
    `mode ∈ {raw,null,bucket}`、bucket 正整数 `width`（显式拒绝 bool/浮点/零/负）、
    重复身份与未知键；旧 revision 永久保留。
  - `compile(roles, revision=None)`：返回不可变 `CompiledPlan`，含绑定 schema、
    revision 与每列一个 `ColumnPlan`。无匹配策略按 null；null 强于 bucket 强于
    raw；多个 bucket 取最大 width。策略只声明列与标量模式，不引用其他列或代码。
- `sql_parser.py`：零依赖分词 + 递归下降解析 + 白名单校验。允许单表 SELECT、
  INNER/LEFT JOIN、WHERE 比较/IS [NOT] NULL/AND/OR、GROUP BY、COUNT/SUM、
  ORDER BY、别名与一层非递归 CTE；其余语法（多语句、子查询、PRAGMA、系统表、
  其他函数、字符串/浮点字面量等）在原库执行前拒绝。
- `query_gateway.py`
  - 保留 `open_seed()` 与 `fixed_rows(db)`。
  - `Gateway(source, catalog)`：`source` 可为连接或文件路径（路径以 `mode=ro`
    只读 URI 打开）。
  - `prepare(sql)` 返回可跨角色重用的 `PreparedQuery`（准备期在空 schema 上
    EXPLAIN 完成名称解析，不读业务数据）。
  - `run(prepared, roles, revision=None)`：绑定一个 revision 后，在全新隔离内存
    库按列计划物化两张基表（策略仅在此应用一次），随后所有过滤、连接、分组、
    聚合都在转换后的关系上求值；CTE 是隔离库内的派生关系，不会再次套策略。
  - 返回 `QueryResult(columns, rows, revision, roles, plan_summary)`；摘要不替代
    真实行结果。未指定 revision 时取发布时的最新版本，已执行的 run 不受运行期间
    新发布影响。

## 测试

- `tests/test_policy_catalog.py`：发布校验、revision 保留、组合规则与计划结构。
- `tests/test_cross_check.py`：独立手工转换的小库（不经过目录/网关代码）对同一批
  SQL 逐行核对；含 null 身份 `WHERE salary=5000` 零命中、`COUNT(salary)=0`、
  bucket 1000 下 950/1000 分桶、双角色交错复用同一 plan、发布期间 revision 钉住。
- `tests/test_gateway_sql.py`：SQL 白名单拒绝面、准备期失败、重复行/同名输出列、
  文件源只读打开与 schema 不一致检测。
