"""Access saved queries as SQL text: a query's MSysQueries rows rebuilt the way Jackcess writes them. No query is run.

Ported from Jackcess 5.0.4 (Apache-2.0; the Maven Central sources jar), `com/healthmarketscience/jackcess/impl/query/`:
`QueryImpl.java` (`create` and its type checks, `sortByOrder`, the row getters and `getUniqueRow`, `toSQLString`, the
parameters with `toParameterName`, `getFromTables` with its `SimpleTable` and `Join` sources and `addCondition`,
`getSelectType`, `toOptionalQuotedExpr`, `toQuotedExpr`, `toRemoteDb` and `toAlias`), `QueryFormat.java` (the
attributes, flags, parameter types and join types), `BaseSelectQueryImpl.java`, `SelectQueryImpl.java`,
`MakeTableQueryImpl.java`, `AppendQueryImpl.java`, `UpdateQueryImpl.java`, `DeleteQueryImpl.java`,
`CrossTabQueryImpl.java`, `DataDefinitionQueryImpl.java`, `PassthroughQueryImpl.java` and `UnionQueryImpl.java`.

`sql(rows, flags=, connect=)` is Jackcess's `toSQLString`: lines joined with "\\n" (Jackcess's line separator), the
text of a SELECT (1), make-table (2), append (3), update (4), delete (5), crosstab (6), union (9) ending with ";", a
DDL (7) or pass-through (8) query its stored text as it is (the NULs that end it dropped). The type is the object
flag's (`flags & 0xF0`, MSysObjects' Flags) or, when that is 0, the type row's, and the two must agree. Where Java
appends a null, "null" is written, as Jackcess does. None when Jackcess would throw (an unknown type, a parameter or
join type it does not know, a second row where one is read, a union part or a target missing, a join naming no table,
no type or no condition), and also for: no type row; more than `JET_QUERY_ROWS` rows; joins nested past
`limits.MAX_DEPTH` (Jackcess recurses without a bound); text past `JET_SQL` characters. Both caps are read at call
time.

Credentials. With `connect`, every connect string and database path a query names is passed through it before it is
written (a remote database's path and type, `IN '<path>' [<type>]`, of a source or of a make-table or append target;
a source's database qualifier, `[<qualifier>].<table>`), and so is the content of every bracketed or quoted run of the
finished text holding ";" and "=" (one left open runs to the end), so a connect string typed into a union part, a
derived table or an expression is cleaned too. A pass-through query's stored text is shown as stored, and its connect
string (the type row's Name1) is never written.

Work. `work(rows)` is an upper bound on what `sql` does, in steps (a character of text, `_ROW` a row, a comparison of
the sort, `_JOIN` a table source a join looks at, builds or walks); the caller charges it before `sql` runs.
"""
import re
from dataclasses import dataclass

from .. import limits

JET_QUERY_ROWS = 10_000
JET_SQL = 65_536

SELECT, MAKE_TABLE, APPEND, UPDATE, DELETE, CROSSTAB, DDL, PASSTHROUGH, UNION = range(1, 10)
_OBJECT_FLAG = {SELECT: 0, MAKE_TABLE: 80, APPEND: 64, UPDATE: 48, DELETE: 32, CROSSTAB: 16, DDL: 96, PASSTHROUGH: 112,
                UNION: 128}  # Query.Type: the object flag of each type value
_TYPE_OF_FLAG = {f: t for t, f in _OBJECT_FLAG.items()}
TYPE, PARAMETER, FLAG, REMOTEDB, TABLE, COLUMN, JOIN, WHERE, GROUPBY, HAVING, ORDERBY = range(1, 12)
_SELECT_STAR, _DISTINCT, _OWNER_ACCESS, _DISTINCT_ROW, _TOP, _PERCENT = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20
_UNION_FLAG = 0x02
_APPEND_VALUE = 0x8000
_PIVOT, _NORMAL = 0x01, 0x02  # a crosstab column row: the pivot, or a normal column (else the TRANSFORM value)
_TEXT = 10
_UNION_PARTS = ("X7YZ_____1", "X7YZ_____2")
_PARAM_TYPES = {0: "Value", 1: "Bit", 10: "Text", 2: "Byte", 3: "Short", 4: "Long", 5: "Currency", 6: "IEEESingle",
                7: "IEEEDouble", 8: "DateTime", 9: "Binary", 11: "LongBinary", 15: "Guid"}
_JOIN_TYPES = {1: " INNER JOIN ", 2: " LEFT JOIN ", 3: " RIGHT JOIN "}
_QUOTABLE = re.compile(r"[^A-Za-z0-9_]")  # Java's \W is ASCII
_LINES = re.compile(r"[\r\n]+")
_OPENS = re.compile(r"[\[\"']")
_JAVA_SPACE = "".join(map(chr, range(33)))  # what Java's trim() takes off
_ROW = 32  # steps a row costs beyond its text: its getters, filters, quoting and separators
_JOIN = 4  # steps a table source costs a join row: a look in the search, and one in each level a condition goes down


@dataclass(slots=True)
class Row:
    """One MSysQueries row (`QueryImpl.Row`): `extra` is LvExtra (a Text parameter's size)."""
    attribute: int | None
    expression: str | None
    flag: int | None
    name1: str | None
    name2: str | None
    object_id: int | None
    order: bytes | None
    extra: int | None = None


_EMPTY = Row(None, None, None, None, None, None, None)


class _Fail(Exception):
    """What Jackcess throws for these rows (IllegalStateException, NullPointerException, UnsupportedOperation)."""


def _short(v):
    """A Java short (MSysQueries' Flag, or a test's number for it)."""
    return (v + 0x8000 & 0xFFFF) - 0x8000


def _s(v) -> str:
    return "null" if v is None else v


def work(rows) -> int:
    """An upper bound on the steps `sql(rows)` takes (see the module's docstring)."""
    n = len(rows)
    chars = sum(len(s) for r in rows for s in (r.expression, r.name1, r.name2) if s)
    tables = sum(1 for r in rows if r.attribute == TABLE)
    joins = n - sum(1 for r in rows if r.attribute != JOIN)
    nodes = tables + 3 * joins
    return 4 * chars + _ROW * n + n * n.bit_length() + _JOIN * joins * nodes * (2 + min(joins, limits.MAX_DEPTH))


def sql(rows, *, flags: int = 0, connect=None) -> str | None:
    """The query's SQL text, or None (see the module's docstring)."""
    if len(rows) > JET_QUERY_ROWS:
        return None
    try:
        q = _Query(rows, flags, connect)
        text = q.text()
    except _Fail:
        return None
    if connect is not None and q.kind != PASSTHROUGH:
        text = _scrub(text, connect)
    return text if len(text) <= JET_SQL else None


def _scrub(text: str, connect) -> str:
    """`text` with the content of each [bracketed], 'quoted' or "quoted" run holding ";" and "=" passed through
    `connect` (a doubled quote is part of its run; a run left open ends with the text). One pass."""
    out, i = [], 0
    while True:
        m = _OPENS.search(text, i)
        if m is None:
            break
        o = m.start()
        c = text[o]
        end = text.find("]", o + 1) if c == "[" else _closing(text, o + 1, c)
        inner = text[o + 1:] if end < 0 else text[o + 1:end]
        out.append(text[i:o + 1])
        out.append(connect(inner) if ";" in inner and "=" in inner else inner)
        if end < 0:
            return "".join(out)
        out.append(text[end])
        i = end + 1
    out.append(text[i:])
    return "".join(out)


def _closing(text: str, at: int, q: str) -> int:
    """Where the quote `q` opened before `at` closes (a doubled one escaped), or -1."""
    while True:
        k = text.find(q, at)
        if k < 0 or text[k + 1:k + 2] != q:
            return k
        at = k + 2


def _split(s: str) -> list:
    """Java's `s.split("\\\\.")`: trailing empty parts dropped, the whole string when there is no "."."""
    parts = s.split(".")
    if len(parts) == 1:
        return parts
    while parts and not parts[-1]:
        parts.pop()
    return parts


def _quoted(e: str) -> str:
    return e if len(e) >= 2 and e[0] == "[" and e[-1] == "]" else f"[{e}]"


def _opt_quoted(full, identifier: bool) -> str:
    if full is None:
        raise _Fail("null name")
    return ".".join(_quoted(e) if _QUOTABLE.search(e) else e for e in (_split(full) if identifier else [full]))


def _alias(alias) -> str:
    return "" if alias is None else " AS " + _opt_quoted(alias, False)


def _param_name(name) -> str:
    """`toParameterName`: a control reference ("Forms!My Form!Field") has each part quoted, any other name whole."""
    if name is None:
        return "null"
    parts, seps, bang, start, inside = [], [], False, 0, False
    for i, c in enumerate(name):
        if c == "[":
            inside = True
        elif c == "]":
            inside = False
        elif not inside and c in "!.":
            bang |= c == "!"
            parts.append(name[start:i])
            seps.append(c)
            start = i + 1
    parts.append(name[start:])
    if not bang or any(not p or p[0] == " " or p[-1] == " " for p in parts):
        return _opt_quoted(name, False)
    return parts and "".join((seps[i - 1] if i else "") + _opt_quoted(p, False) for i, p in enumerate(parts))


class _Simple:
    """A table source that is one table: `low` its name (alias, else name) without case."""
    __slots__ = ("low", "text", "depth")

    def __init__(self, name, text):
        self.low = None if name is None else name.lower()
        self.text, self.depth = text, 0

    def contains(self, t):
        if self.low is None:
            raise _Fail("table with no name")
        return self.low == t

    def add_condition(self, t1, t2, kind, on):
        return False


class _Join:
    """Two table sources joined; `on` its conditions in the order they came (written last first)."""
    __slots__ = ("left", "right", "kind", "on", "depth")

    def __init__(self, left, right, kind, on):
        self.left, self.right, self.kind, self.on = left, right, kind, [on]
        self.depth = 1 + max(left.depth, right.depth)
        if self.depth > limits.MAX_DEPTH:
            raise _Fail("joins nested too deep")

    def contains(self, t):
        return self.left.contains(t) or self.right.contains(t)

    def add_condition(self, t1, t2, kind, on):
        """On the innermost join holding both tables with this join type (the jar's `addCondition`)."""
        for side in (self.left, self.right):
            if side.contains(t1) and side.contains(t2) and side.add_condition(t1, t2, kind, on):
                return True
        if self.kind == kind:
            self.on.append(on)
            return True
        return False


def _render(ts, top: bool, out: list):
    if isinstance(ts, _Simple):
        out.append(ts.text)
        return
    kind = _JOIN_TYPES.get(ts.kind)
    if kind is None or None in ts.on:
        raise _Fail("unknown join type or no condition")
    if not top:
        out.append("(")
    _render(ts.left, False, out)
    out.append(kind)
    _render(ts.right, False, out)
    out.append(" ON ")
    many = len(ts.on) > 1
    out.append("(" + ") AND (".join(reversed(ts.on)) + ")" if many else ts.on[0])
    if not top:
        out.append(")")


class _Query:
    def __init__(self, rows, flags: int, connect):
        if all(r.order is not None for r in rows):  # sortByOrder: by attribute (a Java byte), then Order, stable
            rows = sorted(rows, key=lambda r: (-1 if r.attribute is None else (r.attribute + 128 & 0xFF) - 128,
                                               r.order))
        self.by = {}
        for r in rows:
            self.by.setdefault(r.attribute, []).append(r)
        first = self.by.get(TYPE, (_EMPTY,))[0]
        if first is _EMPTY:
            raise _Fail("no type row")
        found = None if first.flag is None else _short(first.flag)
        obj = flags & 0xF0
        if obj == 0:  # create(): the rows may tell another story
            obj = _OBJECT_FLAG.get(0 if found is None else found, obj)
        self.kind = _TYPE_OF_FLAG.get(obj)
        if self.kind is None:
            raise _Fail(f"unknown query object flag {obj}")
        if (self.kind if found is None else found) != self.kind:
            raise _Fail(f"unexpected query type {found}")
        self.connect = connect or (lambda s: s)

    # -- rows

    def rows(self, attr) -> list:
        return self.by.get(attr, [])

    def one(self, attr) -> Row:
        rows = self.by.get(attr, ())
        if len(rows) > 1:
            raise _Fail("unexpected number of rows")
        return rows[0] if rows else _EMPTY

    def has(self, mask: int) -> bool:
        flag = self.one(FLAG).flag
        return bool(_short(flag or 0) & mask)

    def type_text(self):
        text = self.one(TYPE).expression
        return None if text is None else text.rstrip("\0")

    # -- pieces

    def text(self) -> str:
        kind = self.kind
        if kind in (DDL, PASSTHROUGH):
            return self.type_text() or ""
        out = []
        params = self.parameters()
        if params:
            out.append(f"PARAMETERS {', '.join(params)};\n")
        if kind == UNION:
            self.union(out)
        elif kind == UPDATE:
            self.update(out)
        elif kind == APPEND:
            self.append(out)
        elif kind == DELETE:
            out.append("DELETE ")
            self.select_type(out)
            self.select(out, False)
        elif kind == CROSSTAB:
            self.crosstab(out)
        else:  # SELECT, MAKE_TABLE
            self.select(out, True)
        if self.has(_OWNER_ACCESS):
            out.append("\nWITH OWNERACCESS OPTION")
        out.append(";")
        return "".join(out)

    def parameters(self) -> list:
        out = []
        for r in self.rows(PARAMETER):
            flag = None if r.flag is None else _short(r.flag)
            name = _PARAM_TYPES.get(flag)
            if name is None:
                raise _Fail(f"unknown param type {flag}")
            s = f"{_param_name(r.name1)} {name}"
            if flag == _TEXT and (r.extra or 0) > 0:
                s += f"({r.extra})"
            out.append(s)
        return out

    def select_type(self, out):
        s = "DISTINCT" if self.has(_DISTINCT) else "DISTINCTROW" if self.has(_DISTINCT_ROW) else ""
        if self.has(_TOP):
            s += (" " if s else "") + "TOP " + _s(self.one(FLAG).name1) + (" PERCENT" if self.has(_PERCENT) else "")
        if s:
            out.append(s + " ")

    def remote(self, path, kind) -> str:
        """`toRemoteDb`: " IN '<path>' [<type>]", both through `connect`."""
        if path is None and kind is None:
            return ""
        s = " IN '" + ("" if path is None else self.connect(path)) + "'"
        return s + ("" if kind is None else f" [{self.connect(kind)}]")

    def column_rows(self) -> list:
        rows = self.rows(COLUMN)
        if self.kind == CROSSTAB:
            return [r for r in rows if _short(r.flag or 0) & _NORMAL]
        if self.kind == APPEND:
            return [r for r in rows if not _short(r.flag or 0) & _APPEND_VALUE]
        return rows

    def select_columns(self) -> list:
        out = [_s(r.expression) + _alias(r.name1) for r in self.column_rows()]
        if self.has(_SELECT_STAR):
            out.append("*")
        if not out and self.kind == DELETE:
            out.append("*")
        return out

    def select(self, out, prefix: bool):
        """`toSQLSelectString`."""
        if prefix:
            out.append("SELECT ")
            if self.kind != CROSSTAB:  # the jar leaves a crosstab's stored TOP out of its text
                self.select_type(out)
        out.append(", ".join(self.select_columns()))
        if self.kind == MAKE_TABLE:
            t = self.one(TYPE)
            out.append(" INTO " + _opt_quoted(t.name1, True) + self.remote(t.name2, t.expression))
        tables = self.from_tables()
        if tables:
            db = self.one(REMOTEDB)
            out.append("\nFROM " + ", ".join(tables) + self.remote(db.name1, db.expression))
        where = self.one(WHERE).expression
        if where is not None:
            out.append("\nWHERE " + where)
        groups = self.rows(GROUPBY)
        if self.kind == CROSSTAB:
            groups = [r for r in groups if _short(r.flag or 0) & _NORMAL]
        if groups:
            out.append("\nGROUP BY " + ", ".join(_s(r.expression) for r in groups))
        having = self.one(HAVING).expression
        if having is not None:
            out.append("\nHAVING " + having)
        self.orderings(out)

    def orderings(self, out):
        rows = self.rows(ORDERBY)
        if rows:
            out.append("\nORDER BY " + ", ".join(
                _s(r.expression) + (" DESC" if r.name1 is not None and r.name1.lower() == "d" else "") for r in rows))

    def from_tables(self) -> list:
        """`getFromTables`: the sources, then each join row combining the ones holding its two tables."""
        sources = []
        for t in self.rows(TABLE):
            if t.name1 is None:  # a derived table, in parentheses
                s = "" if t.expression is None else f"({t.expression})"
            else:
                s = ("" if t.expression is None else _quoted(self.connect(t.expression)) + ".")
                s += _opt_quoted(t.name1, True)
            sources.append(_Simple(t.name2 if t.name2 is not None else t.name1, s + _alias(t.name2)))
        for j in self.rows(JOIN):
            if j.name1 is None or j.name2 is None or j.flag is None:
                raise _Fail("join row without its tables or type")
            f, t, kind = j.name1.lower(), j.name2.lower(), _short(j.flag)
            left = right = None
            i = 0
            while i < len(sources) and (left is None or right is None):
                ts = sources[i]
                if left is None and ts.contains(f):
                    left = ts
                    if right is None and ts.contains(t):
                        right = ts
                        break
                    sources.pop(i)
                    continue
                if right is None and ts.contains(t):
                    right = ts
                    sources.pop(i)
                    continue
                i += 1
            left = left or _Simple(j.name1, _opt_quoted(j.name1, True))
            right = right or _Simple(j.name2, _opt_quoted(j.name2, True))
            if left is right:
                if left.add_condition(f, t, kind, j.expression):
                    continue
                raise _Fail(f"inconsistent join types for {j.name1} and {j.name2}")
            sources.append(_Join(left, right, kind, j.expression))
        out = []
        for ts in sources:
            parts = []
            _render(ts, True, parts)
            out.append("".join(parts))
        return out

    # -- the query types beyond SELECT

    def union(self, out):
        def part(name):
            for r in self.rows(TABLE):
                if r.name2 == name:
                    if r.expression is None:
                        raise _Fail("union part with no text")
                    return _LINES.sub("\n", r.expression.strip(_JAVA_SPACE))
            raise _Fail(f"could not find union query with id {name}")
        out.append(part(_UNION_PARTS[0]) + "\nUNION ")
        if not self.has(_UNION_FLAG):
            out.append("ALL ")
        out.append(part(_UNION_PARTS[1]))
        self.orderings(out)

    def update(self, out):
        out.append("UPDATE ")
        self.select_type(out)
        db = self.one(REMOTEDB)
        out.append(", ".join(self.from_tables()) + self.remote(db.name1, db.expression))
        out.append("\nSET " + ", ".join(_opt_quoted(r.name2, True) + " = " + _s(r.expression)
                                        for r in self.rows(COLUMN)))
        where = self.one(WHERE).expression
        if where is not None:
            out.append("\nWHERE " + where)

    def append(self, out):
        t = self.one(TYPE)
        out.append("INSERT INTO " + _opt_quoted(t.name1, True))
        targets = [_opt_quoted(r.name2, True) for r in self.rows(COLUMN) if r.name2 is not None]
        if targets:
            out.append(f" ({', '.join(targets)})")
        out.append(self.remote(t.name2, t.expression) + "\n")
        values = [_s(r.expression) for r in self.rows(COLUMN) if _short(r.flag or 0) & _APPEND_VALUE]
        if values:
            out.append(f"VALUES ({', '.join(values)})")
        else:
            self.select(out, True)

    def crosstab(self, out):
        rows = self.rows(COLUMN)
        value = _unique([r for r in rows if not _short(r.flag or 0) & (_PIVOT | _NORMAL)])
        if value.expression is not None:
            out.append("TRANSFORM " + value.expression + _alias(value.name1) + "\n")
        self.select(out, True)
        out.append("\nPIVOT " + _s(_unique([r for r in rows if _short(r.flag or 0) & _PIVOT]).expression))


def _unique(rows) -> Row:
    if len(rows) > 1:
        raise _Fail("unexpected number of rows")
    return rows[0] if rows else _EMPTY
