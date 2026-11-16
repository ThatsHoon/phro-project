"""Access saved queries as SQL text: a query's MSysQueries rows rebuilt the way Jackcess writes them. No query is run.

Ported from Jackcess 5.0.4 (Apache-2.0; the Maven Central sources jar), `com/healthmarketscience/jackcess/impl/query/`:
`QueryImpl.java` (`create` and its type checks, `sortByOrder`, the row getters and `getUniqueRow`, `toSQLString`, the
parameters with `toParameterName`, `getFromTables` with its `SimpleTable` and `Join` sources and `addCondition`,
`getSelectType`, `toOptionalQuotedExpr`, `toQuotedExpr`, `toRemoteDb` and `toAlias`), `QueryFormat.java` (the
attributes, flags, parameter types and join types), `BaseSelectQueryImpl.java`, `SelectQueryImpl.java`,
`MakeTableQueryImpl.java`, `AppendQueryImpl.java`, `UpdateQueryImpl.java`, `DeleteQueryImpl.java`,
`CrossTabQueryImpl.java`, `DataDefinitionQueryImpl.java`, `PassthroughQueryImpl.java` and `UnionQueryImpl.java`.

`sql(rows, flags=, connect=, urls=)` is Jackcess's `toSQLString`: lines joined with "\\n" (Jackcess's line
separator), the text of a SELECT (1), make-table (2), append (3), update (4), delete (5), crosstab (6), union (9) ending with ";", a
DDL (7) or pass-through (8) query its stored text as it is (the NULs that end it dropped). The type is the object
flag's (`flags & 0xF0`, MSysObjects' Flags) or, when that is 0, the type row's, and the two must agree. Where Java
appends a null, "null" is written, as Jackcess does. None when Jackcess would throw (an unknown type, a parameter or
join type it does not know, a second row where one is read, a union part or a target missing, a join naming no table,
no type or no condition), and also for: no type row; more than `JET_QUERY_ROWS` rows; joins nested past
`limits.MAX_DEPTH` (Jackcess recurses without a bound); text past `JET_SQL` characters. Both caps are read at call
time.

Credentials, by position. With `connect`, every connect position is passed through it: a remote database's path
and type (`IN '<path>' [<type>]`) of a source or of a make-table or append target; a source's database qualifier
(`[<qualifier>].<table>`); a source or target name holding ";", whole, before it is split at "."; and in the finished
text (a union part, a derived table, a subquery, any expression) the quoted or bracketed path after an IN keyword,
the quoted or bracketed type right after that path, and a bracketed prefix holding ";" that a "." follows, spaces,
tabs and line breaks aside (a "{" in it reaches its "}", so a braced value holding "]" stays inside). Every other
quoted literal or bracketed identifier is the document's content and is kept as stored. The scan fails closed: a
quote or bracket left open, a bracket holding "[", or a ";" between runs before the text's end (the parameter list is
read on its own) means the runs are not as Access stores them, so the rest of the text is one run and a connect
position. A pass-through query's stored text has no connect position, and its connect string (the type row's Name1)
is never written. With `urls`, a URL's user part is taken out anywhere: after the connect positions, `urls` is given
the whole text once, pass-through text included (jet.py's hook is `withhold_urls`, which reads one view of the text
without its quotes, brackets, tabs and line breaks, so a URL cut into several runs, or starting "//" or "\\\\", is
withheld from its user part through its "@", whatever runs it crosses). A URL that an expression assembles from
several literals (`'https://u:p' & '@h'`) exists only when the query runs; the reader shows the stored text and never
evaluates it (literals side by side, `'https://u:p' '@h'`, are read as one URL all the same). The hooks are
idempotent; what they withhold they mark ("…").

Work. `work(rows)` is an upper bound on what `sql` does, in steps (a character of text, `_ROW` a row, a comparison of
the sort, `_JOIN` a table source a join looks at, builds or walks); the caller charges it before `sql` runs.
"""
import re
from bisect import bisect_right
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
_BRACKET_STOP = re.compile(r"[\]{]")
_VIEW = re.compile(r"[^'\"\[\]\t\r\n]+")  # what the URL rule reads: no quote, bracket, tab or line break
# where a URL's authority starts: after a special scheme's ":" (slashes or none; group 1, at a word's start), after ":"
# and slashes, or after two or more slashes or backslashes
_URL_START = re.compile(r"(?i)((?:https?|ftp|wss?|file):[/\\]*)|:[/\\]+|[/\\]{2,}")
_AUTH_STOP = re.compile(r"[/\\?#]")  # what ends an authority (a tab or line break never does: WHATWG drops them)
_WORD = re.compile(r"\w")
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


def sql(rows, *, flags: int = 0, connect=None, urls=None) -> str | None:
    """The query's SQL text, or None (see the module's docstring)."""
    if len(rows) > JET_QUERY_ROWS:
        return None
    try:
        q = _Query(rows, flags, connect)
        head, body = q.text()
    except _Fail:
        return None
    if connect is not None and q.kind != PASSTHROUGH:
        head, body = _scrub(head, connect), _scrub(body, connect)
    text = head + body
    if urls is not None and (":" in text or "/" in text or "\\" in text):
        text = urls(text)
    return text if len(text) <= JET_SQL else None


def withhold_urls(text: str):
    """`text` with every URL's user part withheld as one "…", and whether any was (`sql`'s `urls` hook uses it; see the
    module's docstring). One view of the whole text is read: the text without its quotes, brackets, tabs and line
    breaks, each kept stretch mapped back to where it stands. A URL starts in the view (`_URL_START`; a special scheme
    only where the text has no letter, digit or "_" right before it); its authority runs to the next "/", "\\", "?" or
    "#" of the view; its user part to the authority's last "@", or, for a URL that does not start with ":" and slashes
    alone, its last "…" (what a connect position withheld may have held that "@"; so may the document's own "…": it
    hides more). An authority holding ":" and no such end, when the view holds one later (`u:pa/ss@h`), runs to the
    last. From the user part's first character through the character before its "@" (through the "…") the original
    text is withheld, the quotes, brackets, tabs and line breaks between included: so a URL cut into several runs, or
    split by a line break, is withheld whole. An empty user part, or one that is "…" already, is left. One pass:
    each character of the view is read a bounded number of times."""
    starts, at_text, parts = [], [], []
    size = 0
    for m in _VIEW.finditer(text):
        starts.append(size)
        at_text.append(m.start())
        parts.append(m.group())
        size += m.end() - m.start()
    view = "".join(parts)

    def where(v):
        """Where the view's character `v` stands in `text`."""
        k = bisect_right(starts, v) - 1
        return at_text[k] + v - starts[k]

    last_at, last_mark = view.rfind("@"), view.rfind("…")
    spans, pos = [], 0
    while True:
        m = _URL_START.search(view, pos)
        if m is None:
            break
        if m.group(1):
            o = where(m.start())
            if o and _WORD.match(text, o - 1):  # a scheme inside a word ("news:"): not one
                pos = m.start() + 1
                continue
        a = m.end()
        stop = _AUTH_STOP.search(view, a)
        b = size if stop is None else stop.start()
        at = view.rfind("@", a, b)
        if m.group().startswith(":") and not m.group(1):  # ":" and slashes alone (a drive, "c:\\x"): "@" only
            mark, end_at = -1, last_at
        else:
            mark, end_at = view.rfind("…", a, b), max(last_at, last_mark)
        at = max(at, mark)
        if at < 0 and end_at >= b and view.find(":", a, b) >= 0:
            at = end_at
        pos = max(b, at + 1)
        if at < 0:
            continue
        end = at + 1 if view[at] == "…" else at
        if a < end and view[a:end] != "…":
            spans.append((where(a), where(at) + (end - at)))
    if not spans:
        return text, False
    out, last = [], 0
    for s, e in spans:
        out.append(text[last:s])
        out.append("…")
        last = e
    out.append(text[last:])
    return "".join(out), True


def _scrub(text: str, connect) -> str:
    """`text` with each connect position's content passed through `connect` (see the module's docstring). Quoted and
    bracketed runs are read in one pass (a doubled quote is part of its run). It fails closed: a run left open, a
    bracket holding "[", or a ";" between runs before the text's last character make the rest of the text one connect
    position."""
    out, i, gap, path = [], 0, 0, -1
    n = len(text)
    last = len(text.rstrip()) - 1  # where a closing ";" may stand
    while True:
        m = _OPENS.search(text, i)
        o = n if m is None else m.start()
        semi = text.find(";", gap, o)
        if 0 <= semi < last:
            out.append(connect(text[gap:]))
            return "".join(out)
        if m is None:
            break
        c = text[o]
        prefix = False
        if c == "[":
            end = text.find("]", o + 1)
            if end >= 0 and "[" in text[o + 1:end]:
                end = -1
            elif end >= 0 and ";" in text[o + 1:end]:
                end = _bracket_end(text, o + 1)
                if end >= 0:
                    k = end + 1
                    while k < n and text[k] in " \t\r\n":
                        k += 1
                    prefix = text[k:k + 1] == "."
        else:
            end = _closing(text, o + 1, c)
        out.append(text[gap:o])
        out.append(c)
        if end < 0:
            out.append(connect(text[o + 1:]))
            return "".join(out)
        after_in = _after_in(text, o)
        typed = path >= 0 and not text[path:o].strip()
        path = end + 1 if after_in else -1  # a path, quoted or bracketed: a type may follow it
        inner = text[o + 1:end]
        out.append(connect(inner) if after_in or typed or prefix else inner)
        out.append(text[end])
        gap = i = end + 1
    out.append(text[gap:])
    return "".join(out)


def _after_in(text: str, o: int) -> bool:
    """Whether the keyword IN (any case, a whole word) comes before `o`, spaces aside."""
    j = o
    while j > 0 and text[j - 1] in " \t\r\n":
        j -= 1
    return j >= 2 and text[j - 2:j].upper() == "IN" and (j == 2 or not (text[j - 3].isalnum() or text[j - 3] == "_"))


def _bracket_end(text: str, at: int) -> int:
    """Where a bracket opened before `at` closes, a {braced} value (its "}}" escaped) passed over whole; -1 when the
    bracket or a brace is left open."""
    while True:
        m = _BRACKET_STOP.search(text, at)
        if m is None:
            return -1
        if m.group() == "]":
            return m.start()
        at = m.start() + 1
        while True:
            k = text.find("}", at)
            if k < 0:
                return -1
            if text[k + 1:k + 2] != "}":
                at = k + 1
                break
            at = k + 2


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
    return "".join((seps[i - 1] if i else "") + _opt_quoted(p, False) for i, p in enumerate(parts))


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

    def ident(self, name, source: bool) -> str:
        """`toOptionalQuotedExpr(name, true)`, a source or target name holding ";" passed through `connect` first
        (whole, before the split at ".")."""
        if name is not None and source and ";" in name:
            name = self.connect(name)
        return _opt_quoted(name, True)

    # -- pieces

    def text(self) -> tuple:
        """The parameter list ("" when none) and the rest of the text."""
        kind = self.kind
        if kind in (DDL, PASSTHROUGH):
            return "", self.type_text() or ""
        out = []
        params = self.parameters()
        head = f"PARAMETERS {', '.join(params)};\n" if params else ""
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
        return head, "".join(out)

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
            out.append(" INTO " + self.ident(t.name1, True) + self.remote(t.name2, t.expression))
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
                s += self.ident(t.name1, True)
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
            left = left or _Simple(j.name1, self.ident(j.name1, True))
            right = right or _Simple(j.name2, self.ident(j.name2, True))
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
        out.append("\nSET " + ", ".join(self.ident(r.name2, False) + " = " + _s(r.expression)
                                        for r in self.rows(COLUMN)))
        where = self.one(WHERE).expression
        if where is not None:
            out.append("\nWHERE " + where)

    def append(self, out):
        t = self.one(TYPE)
        out.append("INSERT INTO " + self.ident(t.name1, True))
        targets = [self.ident(r.name2, False) for r in self.rows(COLUMN) if r.name2 is not None]
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
