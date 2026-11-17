"""受限 SQL 白名单解析器（无第三方依赖）。

只允许题面列出的语法：单表 SELECT、INNER/LEFT JOIN、WHERE 中的比较 /
IS [NOT] NULL / AND / OR、GROUP BY、COUNT/SUM、ORDER BY、别名与一层
非递归 CTE。其余一切（子查询、多语句、PRAGMA、系统表、附加函数等）在
接触原始数据库之前即拒绝。
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SqlRejectError", "parse", "Program", "Select", "Cte"]


class SqlRejectError(ValueError):
    """SQL 不在允许的只读子集内。"""


# --------------------------------------------------------------------------- #
# 词法
# --------------------------------------------------------------------------- #

_KEYWORDS = frozenset(
    """
    WITH RECURSIVE SELECT DISTINCT ALL FROM AS INNER LEFT OUTER RIGHT FULL
    CROSS JOIN ON WHERE GROUP BY ORDER ASC DESC HAVING LIMIT OFFSET AND OR
    NOT IS NULL UNION INTERSECT EXCEPT INSERT UPDATE DELETE CREATE
    DROP ALTER ATTACH DETACH PRAGMA REPLACE GRANT REVOKE BEGIN COMMIT
    ROLLBACK SAVEPOINT VACUUM REINDEX ANALYZE EXPLAIN VALUES SET TABLE INDEX
    VIEW TRIGGER INTO TRUE FALSE
    """.split()
)

# 聚合名按 SQLite 规则也是普通标识符，仅在函数调用位置识别。
_AGGREGATES = frozenset(("COUNT", "SUM"))

_SIMPLE_TOKENS = {
    "(": "LPAREN",
    ")": "RPAREN",
    ",": "COMMA",
    ".": "DOT",
    "*": "STAR",
    "=": "EQ",
    "+": "PLUS",
    "-": "MINUS",
    ";": "SEMI",
    "<": "LT",
    ">": "GT",
}


@dataclass
class Token:
    kind: str
    value: object
    pos: int


def tokenize(sql: str) -> list[Token]:
    if not isinstance(sql, str):
        raise SqlRejectError("SQL text must be a string")
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\r\n\f\v":
            i += 1
            continue
        if c == "-" and i + 1 < n and sql[i + 1] == "-":
            i = sql.find("\n", i)
            if i == -1:
                break
            continue
        if c == "/" and i + 1 < n and sql[i + 1] == "*":
            end = sql.find("*/", i + 2)
            if end == -1:
                raise SqlRejectError("unterminated block comment")
            i = end + 2
            continue
        start = i
        if c.isalpha() or c == "_":
            i += 1
            while i < n and (sql[i].isalnum() or sql[i] in "_$"):
                i += 1
            word = sql[start:i]
            upper = word.upper()
            if upper in _KEYWORDS:
                tokens.append(Token(upper, upper, start))
            else:
                # 未加引号的标识符按 SQLite 规则大小写不敏感，统一小写。
                tokens.append(Token("IDENT", word.lower(), start))
            continue
        if c.isdigit():
            i += 1
            while i < n and sql[i].isdigit():
                i += 1
            if i < n and sql[i] == ".":
                raise SqlRejectError(
                    f"non-integer numeric literal at position {start}"
                )
            value = int(sql[start:i])
            if value > 2**63 - 1:
                raise SqlRejectError(
                    f"integer literal exceeds int64 range at position {start}"
                )
            tokens.append(Token("NUMBER", value, start))
            continue
        if c in "\"`[":
            close = {"\"": "\"", "`": "`", "[": "]"}[c]
            i += 1
            buf: list[str] = []
            while i < n:
                ch = sql[i]
                if ch == close:
                    if c == "[" or i + 1 >= n or sql[i + 1] != close:
                        i += 1
                        break
                    buf.append(ch)
                    i += 2
                else:
                    buf.append(ch)
                    i += 1
            else:
                raise SqlRejectError(f"unterminated quoted identifier at {start}")
            name = "".join(buf)
            if not name:
                raise SqlRejectError(f"empty quoted identifier at {start}")
            tokens.append(Token("IDENT", name, start))
            continue
        if c == "'":
            raise SqlRejectError(f"string literals are not allowed (position {start})")
        if c in "<>!":
            two = sql[i : i + 2]
            if two in ("<=", ">=", "<>", "!="):
                kind = {"<=": "LE", ">=": "GE", "<>": "NE", "!=": "NE"}[two]
                tokens.append(Token(kind, kind, start))
                i += 2
                continue
            if c in "<>":
                tokens.append(Token(_SIMPLE_TOKENS[c], c, start))
                i += 1
                continue
            raise SqlRejectError(f"unexpected character {c!r} at position {start}")
        if c in _SIMPLE_TOKENS:
            tokens.append(Token(_SIMPLE_TOKENS[c], c, start))
            i += 1
            continue
        raise SqlRejectError(f"unexpected character {c!r} at position {start}")
    tokens.append(Token("EOF", None, n))
    return tokens


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Star:
    pass


@dataclass(frozen=True)
class Column:
    name: str
    table: str | None = None


@dataclass(frozen=True)
class Literal:
    value: int | None  # None 表示 NULL


@dataclass(frozen=True)
class Aggregate:
    name: str  # 'COUNT' | 'SUM'
    arg: Column | Star


@dataclass(frozen=True)
class IsNull:
    operand: "Column | Literal"
    negated: bool


@dataclass(frozen=True)
class Compare:
    left: "Column | Literal"
    op: str
    right: "Column | Literal"


@dataclass(frozen=True)
class BoolOp:
    op: str  # 'AND' | 'OR'
    operands: tuple


@dataclass(frozen=True)
class SelectItem:
    value: Star | Column | Aggregate
    alias: str | None = None


@dataclass(frozen=True)
class TableRef:
    name: str
    alias: str | None

    @property
    def correlation(self) -> str:
        return self.alias or self.name


@dataclass(frozen=True)
class Join:
    kind: str  # 'INNER' | 'LEFT'
    table: TableRef
    on: object


@dataclass(frozen=True)
class OrderItem:
    key: Column | int | str  # 列 / 输出序号 / 输出别名
    desc: bool = False


@dataclass(frozen=True)
class Select:
    items: tuple[SelectItem, ...]
    from_: TableRef
    joins: tuple[Join, ...]
    where: object
    group_by: tuple[Column | int | str, ...]
    order_by: tuple[OrderItem, ...]


@dataclass(frozen=True)
class Cte:
    name: str
    body: Select


@dataclass(frozen=True)
class Program:
    ctes: tuple[Cte, ...]
    select: Select


# --------------------------------------------------------------------------- #
# 递归下降解析
# --------------------------------------------------------------------------- #

_COMPARATORS = {"EQ": "=", "LT": "<", "GT": ">", "LE": "<=", "GE": ">=", "NE": "<>"}


class _Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.pos + offset, len(self.tokens) - 1)]

    def take(self) -> Token:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def accept(self, kind: str) -> Token | None:
        if self.peek().kind == kind:
            return self.take()
        return None

    def expect(self, kind: str, what: str) -> Token:
        tok = self.peek()
        if tok.kind != kind:
            raise SqlRejectError(
                f"expected {what} at position {tok.pos}, got {tok.kind.lower()}"
            )
        return self.take()

    def reject(self, tok: Token, message: str) -> None:
        raise SqlRejectError(f"{message} at position {tok.pos}")

    # -- 顶层 -------------------------------------------------------------- #

    def parse_program(self) -> Program:
        first = self.peek()
        if first.kind not in ("SELECT", "WITH"):
            raise SqlRejectError(
                "only a single SELECT (optionally preceded by one non-recursive "
                f"WITH clause) is allowed; rejected at position {first.pos}"
            )
        ctes: tuple[Cte, ...] = ()
        if self.accept("WITH"):
            if self.accept("RECURSIVE"):
                raise SqlRejectError("recursive CTEs are not allowed")
            cte_list: list[Cte] = []
            while True:
                name_tok = self.expect("IDENT", "CTE name")
                self.expect("AS", "AS after CTE name")
                self.expect("LPAREN", "(")
                if self.peek().kind == "WITH":
                    raise SqlRejectError("nested WITH clauses are not allowed")
                body = self.parse_select()
                self.expect("RPAREN", ") after CTE body")
                cte_list.append(Cte(name=name_tok.value, body=body))
                if not self.accept("COMMA"):
                    break
            ctes = tuple(cte_list)
        select = self.parse_select()
        self.accept("SEMI")
        self.expect("EOF", "end of statement")
        return Program(ctes=ctes, select=select)

    # -- SELECT ------------------------------------------------------------ #

    def parse_select(self) -> Select:
        self.expect("SELECT", "SELECT")
        if self.peek().kind in ("DISTINCT", "ALL"):
            tok = self.take()
            self.reject(tok, f"{tok.kind} is not supported")
        items = tuple(self._parse_select_items())
        self.expect("FROM", "FROM")
        from_ = self.parse_table_ref()
        joins = tuple(self._parse_joins())
        where = None
        if self.accept("WHERE"):
            where = self.parse_expr()
        group_by: tuple = ()
        if self.accept("GROUP"):
            self.expect("BY", "BY after GROUP")
            group_by = tuple(self._parse_group_or_order_keys())
        order_by: tuple = ()
        if self.peek().kind in ("HAVING", "LIMIT", "OFFSET", "UNION",
                                "INTERSECT", "EXCEPT"):
            tok = self.take()
            self.reject(tok, f"{tok.kind} clause is not supported")
        if self.accept("ORDER"):
            self.expect("BY", "BY after ORDER")
            order_items = []
            while True:
                order_items.append(self._parse_order_item())
                if not self.accept("COMMA"):
                    break
            order_by = tuple(order_items)
        if self.peek().kind in ("HAVING", "LIMIT", "OFFSET", "UNION",
                                "INTERSECT", "EXCEPT"):
            tok = self.take()
            self.reject(tok, f"{tok.kind} clause is not supported")
        return Select(
            items=items,
            from_=from_,
            joins=joins,
            where=where,
            group_by=group_by,
            order_by=order_by,
        )

    def _parse_select_items(self) -> list[SelectItem]:
        items: list[SelectItem] = []
        while True:
            if self.accept("STAR"):
                if items:
                    raise SqlRejectError("'*' must be the only select item")
                items.append(SelectItem(Star()))
                if not self.accept("COMMA"):
                    break
                raise SqlRejectError("'*' must be the only select item")
            value = self.parse_select_expr()
            alias = self._parse_optional_alias()
            items.append(SelectItem(value, alias))
            if not self.accept("COMMA"):
                break
        if not items:
            raise SqlRejectError("empty select list")
        return items

    def _parse_optional_alias(self) -> str | None:
        if self.accept("AS"):
            return self.expect("IDENT", "alias name").value
        if self.peek().kind == "IDENT":
            return self.take().value
        return None

    def parse_table_ref(self) -> TableRef:
        name = self.expect("IDENT", "table name").value
        alias = self._parse_optional_alias()
        return TableRef(name=name, alias=alias)

    def _parse_joins(self) -> list[Join]:
        joins: list[Join] = []
        while True:
            tok = self.peek()
            if tok.kind == "COMMA":
                self.reject(tok, "comma-style joins are not allowed; use JOIN")
            kind: str | None = None
            if tok.kind == "INNER":
                self.take()
                kind = "INNER"
            elif tok.kind == "LEFT":
                self.take()
                self.accept("OUTER")
                kind = "LEFT"
            elif tok.kind in ("RIGHT", "FULL", "CROSS"):
                self.reject(tok, f"{tok.kind} JOIN is not allowed")
            if kind is None:
                if not self.accept("JOIN"):
                    break
                kind = "INNER"  # 裸 JOIN 等同 INNER JOIN
            else:
                self.expect("JOIN", "JOIN")
            table = self.parse_table_ref()
            self.expect("ON", "ON after JOIN")
            on = self.parse_expr()
            joins.append(Join(kind=kind, table=table, on=on))
        return joins

    # -- 选择列表表达式（仅列与 COUNT/SUM） --------------------------------- #

    def parse_select_expr(self):
        tok = self.peek()
        if tok.kind == "IDENT" and tok.value.upper() in _AGGREGATES \
                and self.peek(1).kind == "LPAREN":
            self.take()
            name = tok.value.upper()
            self.expect("LPAREN", "(")
            if self.peek().kind in ("DISTINCT", "ALL"):
                t = self.take()
                self.reject(t, f"{t.kind} inside aggregates is not supported")
            if name == "COUNT" and self.accept("STAR"):
                arg: object = Star()
            else:
                arg = self.parse_column()
            self.expect("RPAREN", ")")
            return Aggregate(name=name, arg=arg)
        return self.parse_column()

    def parse_column(self) -> Column:
        first = self.expect("IDENT", "column name")
        table = None
        if self.accept("DOT"):
            table = first.value
            second = self.expect("IDENT", "column name after '.'")
            name = second.value
        else:
            name = first.value
        return Column(name=name, table=table)

    # -- WHERE / ON 布尔表达式 --------------------------------------------- #

    def parse_expr(self):
        node = self.parse_and()
        while self.accept("OR"):
            right = self.parse_and()
            if isinstance(node, BoolOp) and node.op == "OR":
                node = BoolOp("OR", node.operands + (right,))
            else:
                node = BoolOp("OR", (node, right))
        return node

    def parse_and(self):
        node = self.parse_predicate()
        while self.accept("AND"):
            right = self.parse_predicate()
            if isinstance(node, BoolOp) and node.op == "AND":
                node = BoolOp("AND", node.operands + (right,))
            else:
                node = BoolOp("AND", (node, right))
        return node

    def parse_predicate(self):
        if self.accept("NOT"):
            raise SqlRejectError("standalone NOT is not allowed; use IS NOT NULL")
        left = self.parse_operand()
        tok = self.peek()
        if tok.kind == "IS":
            self.take()
            negated = bool(self.accept("NOT"))
            self.expect("NULL", "NULL after IS")
            return IsNull(left, negated)
        if tok.kind in _COMPARATORS:
            self.take()
            right = self.parse_operand()
            return Compare(left, _COMPARATORS[tok.kind], right)
        raise SqlRejectError(
            f"unsupported predicate at position {tok.pos}; only comparisons and "
            "IS [NOT] NULL are allowed"
        )

    def parse_operand(self):
        tok = self.peek()
        sign = 1
        if tok.kind in ("PLUS", "MINUS"):
            if tok.kind == "MINUS":
                sign = -1
            self.take()
            tok = self.peek()
            if tok.kind != "NUMBER":
                raise SqlRejectError(
                    f"signed literal must be an integer at position {tok.pos}"
                )
        if tok.kind == "NUMBER":
            self.take()
            literal = sign * tok.value
            if literal < -(2**63):
                raise SqlRejectError(
                    f"integer literal exceeds int64 range at position {tok.pos}"
                )
            return Literal(literal)
        if tok.kind == "NULL":
            self.take()
            if sign == -1:
                raise SqlRejectError("NULL cannot carry a sign")
            return Literal(None)
        if sign == -1:
            raise SqlRejectError("only integer literals may be signed")
        return self.parse_column()

    # -- GROUP / ORDER 键 --------------------------------------------------- #

    def _parse_group_or_order_keys(self) -> list[Column | int | str]:
        keys: list[Column | int | str] = []
        while True:
            tok = self.peek()
            if tok.kind == "NUMBER":
                self.take()
                if tok.value < 1:
                    self.reject(tok, "ordinal position must be >= 1")
                keys.append(tok.value)
            elif tok.kind == "IDENT" and self.peek(1).kind != "DOT":
                self.take()
                keys.append(tok.value)
            else:
                keys.append(self.parse_column())
            if not self.accept("COMMA"):
                break
        return keys

    def _parse_order_item(self) -> OrderItem:
        key = self._parse_group_or_order_keys()[0]
        desc = False
        if self.peek().kind in ("ASC", "DESC"):
            desc = self.take().kind == "DESC"
        return OrderItem(key=key, desc=desc)


def parse(sql: str) -> Program:
    return _Parser(tokenize(sql)).parse_program()


# --------------------------------------------------------------------------- #
# 结构校验：表引用边界、聚合位置、序号范围、CTE 层级
# 名称解析（未知列、歧义、别名等）交由准备期的空 schema EXPLAIN 兜底。
# --------------------------------------------------------------------------- #


def _iter_selects(program: Program):
    for cte in program.ctes:
        yield cte.body
    yield program.select


def validate(program: Program, base_tables: frozenset[str]) -> None:
    if not program.ctes:
        for select in _iter_selects(program):
            _validate_select(select, base_tables, cte_names=frozenset())
        return

    cte_names = frozenset(c.name for c in program.ctes)
    if len(cte_names) != len(program.ctes):
        raise SqlRejectError("duplicate CTE names")
    if cte_names & base_tables:
        raise SqlRejectError("CTE name must not shadow a registered base table")

    # CTE 体：一层、非递归，只能引用基表。
    for cte in program.ctes:
        _validate_select(cte.body, base_tables, cte_names, cte_body=True)
    # 外层：基表或本语句声明的 CTE。
    _validate_select(
        program.select,
        base_tables | cte_names,
        cte_names,
        cte_body=False,
    )


def _validate_select(
    select: Select,
    allowed_tables: frozenset[str],
    cte_names: frozenset[str],
    cte_body: bool = False,
) -> None:
    refs = [select.from_] + [j.table for j in select.joins]
    correlations: set[str] = set()
    for ref in refs:
        if ref.name not in allowed_tables:
            if ref.name in cte_names and cte_body:
                raise SqlRejectError(
                    "CTE bodies may only reference base tables (one-level CTE)"
                )
            raise SqlRejectError(f"unknown or unregistered table {ref.name!r}")
        if ref.correlation in correlations:
            raise SqlRejectError(f"duplicate correlation name {ref.correlation!r}")
        correlations.add(ref.correlation)

    n_items = len(select.items)
    for key in select.group_by:
        if isinstance(key, int) and key > n_items:
            raise SqlRejectError(
                f"GROUP BY ordinal {key} out of range (select has {n_items} items)"
            )
    for item in select.order_by:
        if isinstance(item.key, int) and item.key > n_items:
            raise SqlRejectError(
                f"ORDER BY ordinal {item.key} out of range (select has {n_items} items)"
            )

    # 聚合只允许出现在选择列表。
    if select.where is not None:
        _assert_no_aggregate(select.where)
    for join in select.joins:
        _assert_no_aggregate(join.on)


def _assert_no_aggregate(node) -> None:
    if isinstance(node, Aggregate):
        raise SqlRejectError("aggregates are only allowed in the select list")
    if isinstance(node, BoolOp):
        for child in node.operands:
            _assert_no_aggregate(child)
    if isinstance(node, Compare):
        for child in (node.left, node.right):
            if isinstance(child, Aggregate):
                raise SqlRejectError("aggregates are only allowed in the select list")
