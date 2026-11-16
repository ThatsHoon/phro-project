"""Access saved queries as SQL text (jet_sql): the cases of Jackcess's QueryTest.java, nested joins, credentials in
connect strings, and the queries that cannot be rebuilt.

The cases and their expected text come from Jackcess's `src/test/java/com/healthmarketscience/jackcess/query/
QueryTest.java` (Apache-2.0; the GitHub mirror at be0a2139b087a227d14cafd4ea231306a5a3a862), with Jackcess's line
separator written "\\n". The code is ported from the 5.0.4 sources jar, which differs from that commit where a case
says so (the jar quotes a parameter name that needs it, drops the NULs that end a stored DDL or pass-through text,
puts a join condition on the innermost join holding both tables, and writes DISTINCT with TOP).

Run: python -I -X utf8 tests/test_reader_jet_sql.py
"""
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))

from reader import limits  # noqa: E402
from reader.formats import jet_sql  # noqa: E402
from reader.formats.jet_sql import Row  # noqa: E402

TYPE, PARAMETER, FLAG, REMOTEDB, TABLE, COLUMN, JOIN, WHERE, GROUPBY, HAVING, ORDERBY = range(1, 12)
SELECT, MAKE_TABLE, APPEND, UPDATE, DELETE, CROSSTAB, DDL, PASSTHROUGH, UNION = range(1, 10)
OBJECT_FLAG = {SELECT: 0, MAKE_TABLE: 80, APPEND: 64, UPDATE: 48, DELETE: 32, CROSSTAB: 16, DDL: 96,
               PASSTHROUGH: 112, UNION: 128}
APPEND_VALUE = -32768  # QueryFormat.APPEND_VALUE_FLAG, (short)0x8000
PART1, PART2 = "X7YZ_____1", "X7YZ_____2"


def row(attr, expr=None, flag=None, name1=None, name2=None, extra=None):
    return Row(attr, expr, flag, name1, name2, None, None, extra)


class Q:
    """A query as QueryTest's newQuery builds it: the type row first, then the rows; its object flag the type's."""

    def __init__(self, kind, *rows, type_expr=None, type_name1=None):
        self.kind = kind
        self.rows = [row(TYPE, type_expr, kind, type_name1)] + list(rows)

    def sql(self):
        return jet_sql.sql(self.rows, flags=OBJECT_FLAG[self.kind])

    def add(self, *rows):
        self.rows += rows

    def remove(self, attr):
        self.rows = [r for r in self.rows if r.attribute != attr]

    def replace(self, *rows):
        self.remove(rows[0].attribute)
        self.add(*rows)

    def set_flag(self, flag):
        self.replace(row(FLAG, None, flag))

    def joins(self, *spec):
        """QueryTest's addJoinRows: (from, to, type) triples; condition "Table<from>.f<i> = Table<to>.f<i>"."""
        self.remove(JOIN)
        for i in range(0, len(spec), 3):
            f, t, kind = spec[i:i + 3]
            self.add(row(JOIN, f"Table{f}.f{i} = Table{t}.f{i}", kind, f"Table{f}", f"Table{t}"))


def lines(*parts):
    return "\n".join(parts)


def test_union_query():
    q = Q(UNION, row(TABLE, "Select * from Table1", None, None, PART1),
          row(TABLE, "Select * from Table2", None, None, PART2))
    q.set_flag(3)
    assert q.sql() == lines("Select * from Table1", "UNION Select * from Table2;"), q.sql()
    q.set_flag(1)
    assert q.sql() == lines("Select * from Table1", "UNION ALL Select * from Table2;")
    q.add(row(ORDERBY, "Table1.id"))
    assert q.sql() == lines("Select * from Table1", "UNION ALL Select * from Table2", "ORDER BY Table1.id;")
    q.remove(TABLE)
    assert q.sql() is None  # IllegalStateException: no union part
    # a part's line breaks become one "\n", the part trimmed (QueryTest's real union query has CRLF parts)
    q = Q(UNION, row(TABLE, "Select a\r\nwhere a = 1\r\n\r\n", None, None, PART1), row(TABLE, " Select b ", None,
                                                                                         None, PART2))
    assert q.sql() == lines("Select a", "where a = 1", "UNION ALL Select b;")


def test_passthrough_and_ddl():
    q = Q(PASSTHROUGH, type_expr="Select * from Table1", type_name1="ODBC;")
    assert q.sql() == "Select * from Table1"  # as stored: no ";" added, the connect string not written
    q = Q(DDL, type_expr="Drop table Table1")
    assert q.sql() == "Drop table Table1"
    # the jar drops the NULs that end the stored text (QueryTest at be0a213 still expects them)
    assert Q(PASSTHROUGH, type_expr="ALTER TABLE Table4 DROP COLUMN col5;\0", type_name1="ODBC;").sql() == \
        "ALTER TABLE Table4 DROP COLUMN col5;"
    assert Q(DDL, type_expr="CREATE TABLE Table5 (col1 CHAR, col2 CHAR);\0\0").sql() == \
        "CREATE TABLE Table5 (col1 CHAR, col2 CHAR);"


def test_update_query():
    q = Q(UPDATE, row(TABLE, None, None, "Table1"), row(COLUMN, '"some string"', None, None, "Table1.id"),
          row(COLUMN, "42", None, None, "Table1.col1"))
    assert q.sql() == lines("UPDATE Table1", 'SET Table1.id = "some string", Table1.col1 = 42;'), q.sql()
    q.add(row(WHERE, "(Table1.col2 < 13)"))
    assert q.sql() == lines("UPDATE Table1", 'SET Table1.id = "some string", Table1.col1 = 42',
                            "WHERE (Table1.col2 < 13);")
    # QueryTest's real update query, its parameter name quoted by the jar's toParameterName
    q = Q(UPDATE, row(PARAMETER, None, 10, "User Name"), row(FLAG, None, 0), row(TABLE, None, None, "Table1"),
          row(COLUMN, '"foo"', 0, None, "Table1.col1"), row(COLUMN, "[Table2].[col3]", 0, None, "Table1.col2"),
          row(COLUMN, "[User Name]", 0, None, "[Table2].[col1]"), row(WHERE, "((([Table2].[col1]) Is Not Null))"))
    assert q.sql() == lines("PARAMETERS [User Name] Text;", "UPDATE Table1",
                            'SET Table1.col1 = "foo", Table1.col2 = [Table2].[col3], [Table2].[col1] = [User Name]',
                            "WHERE ((([Table2].[col1]) Is Not Null));"), q.sql()


def test_select_query():
    q = Q(SELECT, row(TABLE, None, None, "Table1"))
    q.set_flag(1)
    assert q.sql() == lines("SELECT *", "FROM Table1;")
    # doTestColumns
    q.add(row(COLUMN, "Table1.id"), row(COLUMN, "Table1.col", None, "Some.Alias"))
    assert q.sql() == lines("SELECT Table1.id, Table1.col AS [Some.Alias], *", "FROM Table1;")
    # doTestSelectFlags
    cols = "Table1.id, Table1.col AS [Some.Alias], *"
    q.set_flag(3)
    assert q.sql() == lines(f"SELECT DISTINCT {cols}", "FROM Table1;")
    q.set_flag(9)
    assert q.sql() == lines(f"SELECT DISTINCTROW {cols}", "FROM Table1;")
    q.set_flag(7)
    assert q.sql() == lines(f"SELECT DISTINCT {cols}", "FROM Table1", "WITH OWNERACCESS OPTION;")
    q.replace(row(FLAG, None, 49, "5"))
    assert q.sql() == lines(f"SELECT TOP 5 PERCENT {cols}", "FROM Table1;")
    q.replace(row(FLAG, None, 0x12, "10"))  # the jar writes DISTINCT with TOP (be0a213 writes DISTINCT alone)
    assert q.sql() == lines("SELECT DISTINCT TOP 10 Table1.id, Table1.col AS [Some.Alias]", "FROM Table1;")
    q.set_flag(0)
    cols = "Table1.id, Table1.col AS [Some.Alias]"
    # doTestParameters
    q.add(row(PARAMETER, None, 3, "INT_VAL"))
    assert q.sql() == lines("PARAMETERS INT_VAL Short;", f"SELECT {cols}", "FROM Table1;")
    q.add(row(PARAMETER, None, 10, "TextVal", extra=50), row(PARAMETER, None, 0, "[Some Value]", extra=50))
    assert q.sql() == lines("PARAMETERS INT_VAL Short, TextVal Text(50), [Some Value] Value;", f"SELECT {cols}",
                            "FROM Table1;")
    q.add(row(PARAMETER, None, -1, "BadVal"))
    assert q.sql() is None  # an unknown parameter type
    q.remove(PARAMETER)
    # doTestTables
    q.add(row(TABLE, None, None, "Table2", "Another Table"), row(TABLE, "Select val from Table3", None, "val",
                                                                 "Table3Val"))
    tables = "Table1, Table2 AS [Another Table], [Select val from Table3].val AS Table3Val"
    assert q.sql() == lines(f"SELECT {cols}", f"FROM {tables};")
    # doTestRemoteDb
    q.add(row(REMOTEDB, None, 2, "other_db.mdb"))
    assert q.sql() == lines(f"SELECT {cols}", f"FROM {tables} IN 'other_db.mdb';")
    q.replace(row(REMOTEDB, "MDB_FILE;", 2, "other_db.mdb"))
    assert q.sql() == lines(f"SELECT {cols}", f"FROM {tables} IN 'other_db.mdb' [MDB_FILE;];")
    q.replace(row(REMOTEDB, "MDB_FILE;", 2, None))
    assert q.sql() == lines(f"SELECT {cols}", f"FROM {tables} IN '' [MDB_FILE;];")
    q.remove(REMOTEDB)
    # doTestJoins
    q.add(row(JOIN, "(Table1.id = [Another Table].id)", 1, "Table1", "Another Table"))
    assert q.sql() == lines(f"SELECT {cols}", "FROM [Select val from Table3].val AS Table3Val, Table1 INNER JOIN "
                            "Table2 AS [Another Table] ON (Table1.id = [Another Table].id);")
    q.add(row(JOIN, "(Table1.id = Table3Val.id)", 2, "Table1", "Table3Val"))
    joined = ("(Table1 INNER JOIN Table2 AS [Another Table] ON (Table1.id = [Another Table].id)) LEFT JOIN "
              "[Select val from Table3].val AS Table3Val ON (Table1.id = Table3Val.id)")
    assert q.sql() == lines(f"SELECT {cols}", f"FROM {joined};")
    q.add(row(JOIN, "(Table1.id = Table3Val.id)", 5, "Table1", "Table3Val"))
    assert q.sql() is None  # inconsistent join types
    q.rows.pop()
    assert q.sql() is not None
    q.add(row(JOIN, "(Table1.id = Table3Val.id)", 1, "BogusTable", "Table3Val"))
    assert q.sql() == lines(f"SELECT {cols}", f"FROM BogusTable INNER JOIN ({joined}) ON (Table1.id = Table3Val.id);")
    q.remove(JOIN)
    # doTestWhereExpression, doTestGroupings, doTestHavingExpression, doTestOrderings
    head = [f"SELECT {cols}", f"FROM {tables}"]
    q.add(row(WHERE, "(Table1.col2 < 13)"))
    assert q.sql() == lines(*head, "WHERE (Table1.col2 < 13);")
    q.add(row(GROUPBY, "Table1.id"), row(GROUPBY, "SUM(Table1.val)"))
    assert q.sql() == lines(*head, "WHERE (Table1.col2 < 13)", "GROUP BY Table1.id, SUM(Table1.val);")
    q.add(row(HAVING, "(SUM(Table1.val) = 500)"))
    assert q.sql() == lines(*head, "WHERE (Table1.col2 < 13)", "GROUP BY Table1.id, SUM(Table1.val)",
                            "HAVING (SUM(Table1.val) = 500);")
    q.add(row(ORDERBY, "Table1.id"), row(ORDERBY, "Table2.val", None, "D"))
    assert q.sql() == lines(*head, "WHERE (Table1.col2 < 13)", "GROUP BY Table1.id, SUM(Table1.val)",
                            "HAVING (SUM(Table1.val) = 500)", "ORDER BY Table1.id, Table2.val DESC;")


def test_bad_queries():
    # an object flag naming no query type (create(-1, ...)): Jackcess's UnknownQueryImpl writes nothing
    assert jet_sql.sql([row(TYPE, None, -1)], flags=-1) is None
    # a type row that disagrees with the object flag (the constructor's IllegalStateException)
    assert jet_sql.sql([row(TYPE, None, -1)], flags=OBJECT_FLAG[UNION]) is None
    # two type rows: the one Jackcess reads must be unique
    assert jet_sql.sql([row(TYPE, "x", DDL), row(TYPE, "y", DDL)], flags=OBJECT_FLAG[DDL]) is None
    # two WHERE (or flag) rows
    assert jet_sql.sql([row(TYPE, None, SELECT), row(WHERE, "a"), row(WHERE, "b")]) is None
    assert jet_sql.sql([row(TYPE, None, SELECT), row(FLAG, None, 0), row(FLAG, None, 0)]) is None
    # a missing make-table or append target, a join row without a table or a type, a join with no condition
    assert jet_sql.sql([row(TYPE, None, MAKE_TABLE), row(COLUMN, "a")]) is None
    assert jet_sql.sql([row(TYPE, None, APPEND), row(COLUMN, "a")]) is None
    assert jet_sql.sql([row(TYPE, None, SELECT), row(TABLE, None, None, "A"), row(JOIN, "x", 1, "A", None)]) is None
    assert jet_sql.sql([row(TYPE, None, SELECT), row(TABLE, None, None, "A"), row(JOIN, "x", None, "A", "B")]) is None
    assert jet_sql.sql([row(TYPE, None, SELECT), row(TABLE, None, None, "A"), row(JOIN, None, 1, "A", "B")]) is None
    # a table row with no name, reached by a join's search (Jackcess's NullPointerException)
    assert jet_sql.sql([row(TYPE, None, SELECT), row(TABLE), row(JOIN, "x", 1, "A", "B")]) is None
    # Java's append(null) writes "null" where Jackcess does
    assert jet_sql.sql([row(TYPE, None, SELECT), row(COLUMN, None), row(ORDERBY, None)]) == \
        lines("SELECT null", "ORDER BY null;")


def test_append_query():
    q = Q(APPEND, row(COLUMN, "54", APPEND_VALUE), row(COLUMN, "'hello'", APPEND_VALUE), type_name1="Table2")
    assert q.sql() == lines("INSERT INTO Table2", "VALUES (54, 'hello');"), q.sql()
    q = Q(APPEND, row(COLUMN, "54", APPEND_VALUE, None, "ID"), row(COLUMN, "'hello'", APPEND_VALUE, None, "Field 3"),
          type_name1="Table2")
    assert q.sql() == lines("INSERT INTO Table2 (ID, [Field 3])", "VALUES (54, 'hello');")
    # an unsigned 0x8000 flag is the same bit
    q = Q(APPEND, row(COLUMN, "54", 0x8000), type_name1="Table2")
    assert q.sql() == lines("INSERT INTO Table2", "VALUES (54);")
    # QueryTest's real append query: a select, its targets from every column row
    q = Q(APPEND, row(FLAG, None, 0), row(COLUMN, "[Table1].[col2]", 0, None, "col2"),
          row(COLUMN, "[Table2].[col2]", 0, None, "col2"), row(COLUMN, "[Table2].[col3]", 0, None, "col3"),
          row(TABLE, None, None, "Table3"), row(JOIN, "[Table1].[col1]=[Table2].[col1]", 1, "Table1", "Table2"),
          row(TABLE, None, None, "Table1"), row(TABLE, None, None, "Table2"), type_name1="Table3")
    assert q.sql() == lines("INSERT INTO Table3 (col2, col2, col3)",
                            "SELECT [Table1].[col2], [Table2].[col2], [Table2].[col3]",
                            "FROM Table3, Table1 INNER JOIN Table2 ON [Table1].[col1]=[Table2].[col1];"), q.sql()


def test_jackcess_query_cases():
    """The other queries of QueryTest's testReadQueries, as rows: make-table, crosstab, delete and select."""
    q = Q(MAKE_TABLE, row(FLAG, None, 0), row(TABLE, None, None, "Table2"), row(TABLE, None, None, "Table1"),
          row(TABLE, None, None, "Table3"), row(COLUMN, "Max(Table2.col1)", 0, "MaxOfcol1"),
          row(GROUPBY, "Table2.col2", 0), row(COLUMN, "Table2.col2", 0), row(GROUPBY, "Table3.col2", 0),
          row(COLUMN, "Table3.col2", 0), row(JOIN, "Table2.col1 = Table1.col2", 1, "Table2", "Table1"),
          row(JOIN, "Table1.col2 = Table3.col3", 3, "Table1", "Table3"),
          row(HAVING, '(((Max(Table2.col1))="buzz") AND ((Table2.col2)<>"blah"))'), type_name1="Table4")
    assert q.sql() == lines(
        "SELECT Max(Table2.col1) AS MaxOfcol1, Table2.col2, Table3.col2 INTO Table4",
        "FROM (Table2 INNER JOIN Table1 ON Table2.col1 = Table1.col2) RIGHT JOIN Table3 ON Table1.col2 = Table3.col3",
        "GROUP BY Table2.col2, Table3.col2",
        'HAVING (((Max(Table2.col1))="buzz") AND ((Table2.col2)<>"blah"));'), q.sql()
    # crosstab: flag 1 the pivot column, 2 a normal column or grouping, neither the TRANSFORM value; rows in their
    # stored order, which `Order` sorts within each attribute
    rows = [(COLUMN, "Count([Table2].[col2])", 0, "CountOfcol2", None, 1),
            (COLUMN, "Table2_1.col1", 2, None, None, 2), (COLUMN, "[Table2].[col3]", 2, None, None, 3),
            (COLUMN, "Avg(Table2_1.col2)", 2, "AvgOfcol2", None, 4),
            (JOIN, "[Table1].[col1]=[Table2].[col1]", 1, "Table1", "Table2", 1),
            (JOIN, "[Table2].[col1]=Table2_1.col3", 1, "Table2", "Table2_1", 2),
            (TABLE, None, None, "Table1", None, 1), (TABLE, None, None, "Table2", None, 2),
            (TABLE, None, None, "Table2", "Table2_1", 3),
            (WHERE, '((([Table1].[col1])>"10") And ((Table2_1.col1) Is Not Null) And ((Avg(Table2_1.col2))>"10"))',
             None, None, None, 1),
            (GROUPBY, "Table2_1.col1", 2, None, None, 1), (GROUPBY, "[Table2].[col3]", 2, None, None, 2),
            (ORDERBY, "[Table2].[col3]", None, None, None, 1), (GROUPBY, "[Table1].[col1]", 1, None, None, 3),
            (COLUMN, "[Table1].[col1]", 1, None, None, 5), (TYPE, None, CROSSTAB, None, None, 1)]
    rows = [Row(a, e, f, n1, n2, 7, o.to_bytes(4, "big")) for a, e, f, n1, n2, o in rows]
    want = lines(
        "TRANSFORM Count([Table2].[col2]) AS CountOfcol2",
        "SELECT Table2_1.col1, [Table2].[col3], Avg(Table2_1.col2) AS AvgOfcol2",
        "FROM (Table1 INNER JOIN Table2 ON [Table1].[col1]=[Table2].[col1]) INNER JOIN Table2 AS Table2_1 ON "
        "[Table2].[col1]=Table2_1.col3",
        'WHERE ((([Table1].[col1])>"10") And ((Table2_1.col1) Is Not Null) And ((Avg(Table2_1.col2))>"10"))',
        "GROUP BY Table2_1.col1, [Table2].[col3]",
        "ORDER BY [Table2].[col3]",
        "PIVOT [Table1].[col1];")
    assert jet_sql.sql(rows, flags=16) == want, jet_sql.sql(rows, flags=16)
    assert jet_sql.sql(list(reversed(rows)), flags=16) == want  # `Order` puts them back
    # the jar leaves a crosstab's stored TOP out of its text
    assert jet_sql.sql(rows + [Row(FLAG, None, 0x10, "5", None, 7, b"\0\0\0\1")], flags=16) == want
    q = Q(DELETE, row(FLAG, None, 0), row(TABLE, None, None, "Table1"), row(COLUMN, "Table1.col1", 0),
          row(COLUMN, "Table1.col2", 0), row(COLUMN, "Table1.col3", 0), row(WHERE, '(((Table1.col1)>"blah"))'))
    assert q.sql() == lines("DELETE Table1.col1, Table1.col2, Table1.col3", "FROM Table1",
                            'WHERE (((Table1.col1)>"blah"));')
    q = Q(DELETE, row(TABLE, None, None, "Table1"), row(WHERE, "x > 1"))  # the jar writes "*" for no columns
    assert q.sql() == lines("DELETE *", "FROM Table1", "WHERE x > 1;")
    q = Q(SELECT, row(FLAG, None, 2), row(TABLE, None, None, "Table1"), row(TABLE, None, None, "Table2"),
          row(TABLE, None, None, "Table3"), row(COLUMN, "Table1.*", 0), row(COLUMN, "Table2.col1", 0),
          row(COLUMN, "Table2.col2", 0), row(COLUMN, "Table3.col3", 0),
          row(JOIN, "Table1.col1 = Table3.col1", 2, "Table1", "Table3"),
          row(JOIN, "Table3.col1 = Table2.col1", 1, "Table3", "Table2"),
          row(JOIN, "Table3.col1 = Table2.col2", 1, "Table3", "Table2"), row(ORDERBY, "Table2.col1"),
          row(WHERE, '(((Table2.col2)="foo" Or (Table2.col2) In ("buzz","bazz")))'))
    assert q.sql() == lines(
        "SELECT DISTINCT Table1.*, Table2.col1, Table2.col2, Table3.col3",
        "FROM (Table1 LEFT JOIN Table3 ON Table1.col1 = Table3.col1) INNER JOIN Table2 ON (Table3.col1 = Table2.col2) "
        "AND (Table3.col1 = Table2.col1)",
        'WHERE (((Table2.col2)="foo" Or (Table2.col2) In ("buzz","bazz")))',
        "ORDER BY Table2.col1;"), q.sql()
    # the type row alone decides when the object flag is 0, as Jackcess's create does
    assert jet_sql.sql([row(TYPE, "DROP TABLE x", DDL)]) == "DROP TABLE x"


def test_complex_joins():
    q = Q(SELECT)
    q.set_flag(1)
    for i in range(1, 11):
        q.add(row(TABLE, None, None, f"Table{i}"))
    q.joins(1, 2, 1, 2, 3, 1, 3, 4, 1)
    assert q.sql() == lines("SELECT *", "FROM Table5, Table6, Table7, Table8, Table9, Table10, ((Table1 INNER JOIN "
                            "Table2 ON Table1.f0 = Table2.f0) INNER JOIN Table3 ON Table2.f3 = Table3.f3) INNER JOIN "
                            "Table4 ON Table3.f6 = Table4.f6;"), q.sql()
    q.joins(1, 2, 1, 2, 1, 1)
    assert q.sql() == lines("SELECT *", "FROM Table3, Table4, Table5, Table6, Table7, Table8, Table9, Table10, Table1 "
                            "INNER JOIN Table2 ON (Table2.f3 = Table1.f3) AND (Table1.f0 = Table2.f0);")
    q.joins(1, 2, 1, 2, 1, 2)
    assert q.sql() is None
    q.joins(1, 2, 1, 3, 4, 1, 5, 6, 1, 7, 8, 1, 2, 3, 1)
    assert q.sql() == lines("SELECT *", "FROM Table9, Table10, Table5 INNER JOIN Table6 ON Table5.f6 = Table6.f6, "
                            "Table7 INNER JOIN Table8 ON Table7.f9 = Table8.f9, (Table1 INNER JOIN Table2 ON Table1.f0 "
                            "= Table2.f0) INNER JOIN (Table3 INNER JOIN Table4 ON Table3.f3 = Table4.f3) ON Table2.f12 "
                            "= Table3.f12;")
    q.joins(1, 2, 1, 3, 4, 1, 5, 6, 1, 7, 8, 1, 2, 3, 1, 5, 8, 1)
    assert q.sql() == lines("SELECT *", "FROM Table9, Table10, (Table1 INNER JOIN Table2 ON Table1.f0 = Table2.f0) "
                            "INNER JOIN (Table3 INNER JOIN Table4 ON Table3.f3 = Table4.f3) ON Table2.f12 = "
                            "Table3.f12, (Table5 INNER JOIN Table6 ON Table5.f6 = Table6.f6) INNER JOIN (Table7 INNER "
                            "JOIN Table8 ON Table7.f9 = Table8.f9) ON Table5.f15 = Table8.f15;")
    q.joins(1, 2, 1, 1, 3, 1, 6, 3, 1, 1, 9, 2, 10, 9, 3, 7, 8, 3, 1, 8, 2, 1, 4, 2, 5, 4, 3)
    assert q.sql() == lines("SELECT *", "FROM Table5 RIGHT JOIN (((Table10 RIGHT JOIN ((Table6 INNER JOIN ((Table1 "
                            "INNER JOIN Table2 ON Table1.f0 = Table2.f0) INNER JOIN Table3 ON Table1.f3 = Table3.f3) "
                            "ON Table6.f6 = Table3.f6) LEFT JOIN Table9 ON Table1.f9 = Table9.f9) ON Table10.f12 = "
                            "Table9.f12) LEFT JOIN (Table7 RIGHT JOIN Table8 ON Table7.f15 = Table8.f15) ON Table1.f18 "
                            "= Table8.f18) LEFT JOIN Table4 ON Table1.f21 = Table4.f21) ON Table5.f24 = Table4.f24;")
    q.joins(1, 2, 1, 1, 3, 1, 1, 9, 2, 1, 8, 2, 1, 4, 2, 6, 3, 1, 5, 4, 3, 7, 8, 3, 10, 9, 3)
    assert q.sql() == lines("SELECT *", "FROM Table10 RIGHT JOIN (Table7 RIGHT JOIN (Table5 RIGHT JOIN (Table6 INNER "
                            "JOIN (((((Table1 INNER JOIN Table2 ON Table1.f0 = Table2.f0) INNER JOIN Table3 ON "
                            "Table1.f3 = Table3.f3) LEFT JOIN Table9 ON Table1.f6 = Table9.f6) LEFT JOIN Table8 ON "
                            "Table1.f9 = Table8.f9) LEFT JOIN Table4 ON Table1.f12 = Table4.f12) ON Table6.f15 = "
                            "Table3.f15) ON Table5.f18 = Table4.f18) ON Table7.f21 = Table8.f21) ON Table10.f24 = "
                            "Table9.f24;")
    q.remove(TABLE)
    q.joins(1, 2, 1, 2, 3, 1, 2, 4, 1, 1, 4, 1, 5, 3, 1)
    assert q.sql() == lines("SELECT *", "FROM Table5 INNER JOIN (((Table1 INNER JOIN Table2 ON Table1.f0 = Table2.f0) "
                            "INNER JOIN Table3 ON Table2.f3 = Table3.f3) INNER JOIN Table4 ON (Table1.f9 = Table4.f9) "
                            "AND (Table2.f6 = Table4.f6)) ON Table5.f12 = Table3.f12;")


def test_nested_joins():
    tables = [row(TABLE, None, None, n) for n in ("Orders", "Customers", "Regions")]
    inner = row(JOIN, "Orders.CustomerID = Customers.ID", 1, "Orders", "Customers")
    left = row(JOIN, "Customers.RegionID = Regions.ID", 2, "Customers", "Regions")
    want = ("FROM (Orders INNER JOIN Customers ON Orders.CustomerID = Customers.ID) LEFT JOIN Regions ON "
            "Customers.RegionID = Regions.ID;")
    assert jet_sql.sql([row(TYPE, None, SELECT), row(COLUMN, "Orders.ID")] + tables + [inner, left]) == \
        lines("SELECT Orders.ID", want)
    # a second INNER condition on Orders and Customers goes on the inner join (the jar's addCondition), in front
    more = row(JOIN, "Orders.Year = Customers.Year", 1, "Customers", "Orders")
    assert jet_sql.sql([row(TYPE, None, SELECT), row(COLUMN, "Orders.ID")] + tables + [inner, left, more]) == \
        lines("SELECT Orders.ID", "FROM (Orders INNER JOIN Customers ON (Orders.Year = Customers.Year) AND "
              "(Orders.CustomerID = Customers.ID)) LEFT JOIN Regions ON Customers.RegionID = Regions.ID;")
    # names compared without case; a quoted name and an alias
    q = [row(TYPE, None, SELECT), row(COLUMN, "o.ID"), row(TABLE, None, None, "My Orders", "o"),
         row(TABLE, None, None, "dbo.Lines"), row(JOIN, "o.ID = Lines.OrderID", 3, "O", "DBO.LINES")]
    assert jet_sql.sql(q) == lines("SELECT o.ID", "FROM [My Orders] AS o RIGHT JOIN dbo.Lines ON o.ID = Lines.OrderID;")
    # a join nesting past limits.MAX_DEPTH is not rebuilt
    deep = [row(TYPE, None, SELECT)] + [row(JOIN, f"c{i}", 1, f"T{i}", f"T{i + 1}")
                                        for i in range(limits.MAX_DEPTH + 1)]
    assert jet_sql.sql(deep) is None
    assert jet_sql.sql(deep[:limits.MAX_DEPTH]) is not None


def test_quoting():
    # Java's \W is ASCII: a non-ASCII letter is quoted; String.split drops trailing empty parts
    assert jet_sql.sql([row(TYPE, None, MAKE_TABLE, "Größe.x."), row(COLUMN, "a")]) == "SELECT a INTO [Größe].x;"
    assert jet_sql.sql([row(TYPE, None, MAKE_TABLE, "."), row(COLUMN, "a")]) == "SELECT a INTO ;"
    assert jet_sql.sql([row(TYPE, None, MAKE_TABLE, "[a.b]"), row(COLUMN, "a")]) == "SELECT a INTO [[a].[b]];"
    # parameter names: a control reference has each part quoted, any other name is quoted whole
    q = [row(TYPE, None, SELECT), row(PARAMETER, None, 4, "Forms!My Form!Field"),
         row(PARAMETER, None, 4, "Enter No. of items"), row(PARAMETER, None, 4, "[Forms]![F].[x y]"),
         row(PARAMETER, None, 4, "a! b")]
    assert jet_sql.sql(q) == lines("PARAMETERS Forms![My Form]!Field Long, [Enter No. of items] Long, "
                                   "[Forms]![F].[x y] Long, [a! b] Long;", "SELECT ;")


def connect_cleaner(s):
    """A stand-in for jet.py's connect cleaner (idempotent, as it is): the text from "PWD" on withheld, as "…"."""
    k = s.find("PWD")
    return s if k < 0 else s[:k].rstrip(".;") + ";…"


def url_cleaner(s):
    """A stand-in for jet.py's URL rule (idempotent), given one quoted or bracketed run's content or a stretch between
    runs: a URL's user part withheld as "…", its authority running to "/", "\\", "?", "#" or the end (a tab or line
    break never ends it)."""
    return re.sub(r"(?i)(https?:/*|//)[^/\\?#]*@", r"\1…@", s)


def test_connect_hook():
    # the connect positions go through `connect`: a remote database's path and type, a source's qualifier, a
    # make-table or append target, and in the text of a union part, a derived table or a subquery an IN clause's
    # path and type and a [connect]. prefix; a pass-through query's stored text never
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(TABLE, "ODBC;DSN=x;PWD=s", None, "T"),
         row(REMOTEDB, "ODBC;DSN=y;PWD=s", None, "\\\\h\\d.mdb")]
    got = jet_sql.sql(q, connect=connect_cleaner)
    assert got == lines("SELECT a", "FROM [ODBC;DSN=x;…].T IN '\\\\h\\d.mdb' [ODBC;DSN=y;…];"), got
    q = [row(TYPE, "ODBC;PWD=s", APPEND, "T", "p.mdb"), row(COLUMN, "1", APPEND_VALUE)]
    assert jet_sql.sql(q, connect=connect_cleaner) == lines("INSERT INTO T IN 'p.mdb' [ODBC;…]", "VALUES (1);")
    q = [row(TYPE, None, UNION), row(TABLE, "Select a from T IN '' [ODBC;DSN=z;PWD=s]", None, None, PART1),
         row(TABLE, "Select 'k;PWD=v' from \"U;x=1\" IN \"c:\\x\" \"dBASE IV;PWD=w\"", None, None, PART2)]
    got = jet_sql.sql(q, connect=connect_cleaner)
    assert got == lines("Select a from T IN '' [ODBC;DSN=z;…]",
                        "UNION ALL Select 'k;PWD=v' from \"U;x=1\" IN \"c:\\x\" \"dBASE IV;…\";"), got
    # a derived table and a subquery: an IN clause, a [connect]. prefix, a brace holding "]" inside a prefix
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(TABLE, "SELECT * FROM T IN 'x;PWD=1'", None, None, "q"),
         row(WHERE, "a IN (SELECT b FROM [ODBC;DSN=a;PWD=1].T) OR c IN (SELECT d FROM "
                    "[ODBC;DRIVER={x};PWD={ab]s3cr3t}].U)")]
    got = jet_sql.sql(q, connect=connect_cleaner)
    assert got == lines("SELECT a", "FROM (SELECT * FROM T IN 'x;…') AS q",
                        "WHERE a IN (SELECT b FROM [ODBC;DSN=a;…].T) OR c IN "
                        "(SELECT d FROM [ODBC;DRIVER={x};…].U);"), got
    # a target or source name holding ";" is cleaned whole, before it is split at "."
    q = [row(TYPE, None, MAKE_TABLE, "ODBC;DSN=a;.PWD=s3cr3t"), row(COLUMN, "a"),
         row(TABLE, None, None, "ODBC;DSN=b;PWD=x.y")]
    got = jet_sql.sql(q, connect=connect_cleaner)
    assert got == lines("SELECT a INTO [ODBC;DSN=a;…]", "FROM [ODBC;DSN=b;…];"), got
    # an IN clause left open runs to the end of the text, cleaned
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, "a IN 'x;PWD=1")]
    assert jet_sql.sql(q, connect=connect_cleaner) == lines("SELECT a", "WHERE a IN 'x;…"), \
        jet_sql.sql(q, connect=connect_cleaner)
    # any other literal or identifier is the document's content, kept as stored
    kept = ["[Notes] Like \"*key=1;*\"", "DLookUp(\"Name\",\"T\",\"[A]=1 AND [B]='x;PWD=y'\") Is Null",
            "[a;PWD=b] = 'IN ''x;PWD=1'' z' AND [x;y=z]"]
    for w in kept:
        q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, w)]
        assert jet_sql.sql(q, connect=connect_cleaner) == lines("SELECT a", f"WHERE {w};"), w
    # a pass-through query's stored text: no connect position; its connect string is never written
    q = [row(TYPE, "SELECT * FROM t IN 'x;PWD=y' WHERE p = '[x;PWD=y]'", PASSTHROUGH, "ODBC;DSN=a;PWD=s")]
    assert jet_sql.sql(q, connect=connect_cleaner) == "SELECT * FROM t IN 'x;PWD=y' WHERE p = '[x;PWD=y]'"
    # without the hook, the text is Jackcess's
    assert jet_sql.sql([row(TYPE, None, SELECT), row(COLUMN, "'a;b=c'"), row(TABLE, "x;PWD=1", None, "T")]) == \
        lines("SELECT 'a;b=c'", "FROM [x;PWD=1].T;")


def test_unbalanced_fails_closed():
    # a quote or bracket left open, a bracket holding "[", or a ";" between runs before the text's end: the runs are
    # not as Access stores them, so the rest of the text is one run, a connect position (and then the URL rule)
    cases = {
        "x = O'Neil AND y IN (SELECT b FROM T IN '' [ODBC;PWD=s3cr3t])": "x = O'Neil AND y IN (SELECT b FROM T IN '' "
                                                                          "[ODBC;…",
        "x = 5\" AND y IN (SELECT b FROM [ODBC;PWD=s3cr3t].T)": "x = 5\" AND y IN (SELECT b FROM [ODBC;…",
        "x = [a AND y IN (SELECT b FROM T IN '' [ODBC;PWD=s3cr3t])": "x = [a AND y IN (SELECT b FROM T IN '' [ODBC;…",
        "x IN (SELECT y FROM [ODBC;DSN=x;PWD=s3cr3t": "x IN (SELECT y FROM [ODBC;DSN=x;…",
        "[ODBC;DSN=x;PWD=s3cr3t AND y = [z]": "[ODBC;DSN=x;…",
        "a = 'x AND b IN 'c:\\db.mdb' ';PWD=s3cr3t'": "a = 'x AND b IN 'c:\\db.mdb' ';…",
        "a = 'https://bob:s3cr3t@h": "a = 'https://…@h;",
    }
    for w, want in cases.items():
        q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, w)]
        got = jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner)
        assert "s3cr3t" not in got and got == lines("SELECT a", f"WHERE {want}"), (w, got)
    # a parameter list's ";" is its own end; a pass-through query's runs are only read for URLs
    q = [row(TYPE, None, SELECT), row(PARAMETER, None, 4, "p;q"), row(COLUMN, "a"), row(WHERE, "a = [p;q]")]
    assert jet_sql.sql(q, connect=connect_cleaner) == lines("PARAMETERS [p;q] Long;", "SELECT a", "WHERE a = [p;q];")
    q = [row(TYPE, "SELECT 1; SELECT 'https://bob:pw@h", PASSTHROUGH, "ODBC;")]
    assert jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner) == "SELECT 1; SELECT 'https://…@h"
    # a [connect] prefix with spaces before its "."
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, "a IN (SELECT b FROM [ODBC;DSN=x;PWD=s3cr3t] .T)")]
    want = lines("SELECT a", "WHERE a IN (SELECT b FROM [ODBC;DSN=x;…] .T);")
    assert jet_sql.sql(q, connect=connect_cleaner) == want
    # ... or a line break
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, "a IN (SELECT b FROM [ODBC;DSN=x;PWD=s3cr3t]\r\n.T)")]
    want = lines("SELECT a", "WHERE a IN (SELECT b FROM [ODBC;DSN=x;…]\r\n.T);")
    assert jet_sql.sql(q, connect=connect_cleaner) == want


def test_url_across_runs():
    # a special scheme's authority that reaches the end of its run or stretch with no "/", "\\", "?" or "#", when
    # the next run or stretch holds an "@" before any of them: withheld from the scheme through that "@"
    cases = {"x = https://bob:s3cr3t'@h'": "x = https://…@h';", "[https://bob:s3cr3t]@h = 1": "[https://…@h = 1;",
             "x = 'https://bob:s3cr3t'@h": "x = 'https://…@h;",
             "x = \"https://bob:s3cr3t\"[@h]": "x = \"https://…@h];"}
    for w, want in cases.items():
        q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, w)]
        got = jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner)
        assert "s3cr3t" not in got and got == lines("SELECT a", f"WHERE {want}"), (w, got)
    q = [row(TYPE, "EXEC p 'https://bob:s3cr3t'@h", PASSTHROUGH, "ODBC;")]
    assert jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner) == "EXEC p 'https://…@h"
    # kept: the next stretch holds no "@", or a stop comes first, or the authority stopped inside its run
    for w in ("x = 'https://h:8080' AND y = 'a@b'", "x = 'https://h:1'/'@b'", "x = 'https://h/'@b",
              "x = 'https://h:1'/a@b",
              "x = 'mailto:bob'@h"):
        q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, w)]
        assert jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner) == lines("SELECT a", f"WHERE {w};"), w


def test_url_hook():
    # `urls` takes a URL's user part out anywhere in the text, pass-through included, and from a name before it is
    # split at "."; it is given each quoted or bracketed run's content and each stretch between runs, so a URL's
    # authority ends with its run, whatever the run holds (a space, a doubled quote, a comma, a bracket)
    w = ("u = 'https://bob:my s3cr3t@h/x' OR v = 'https://bob:it''ss3cr3t@h/x' OR w = 'https:bob:s3cr3t@h/x' "
         "OR z = 'https://bob:pw' AND y = 'a@b'")
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, w)]
    got = jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner)
    assert got == lines("SELECT a", "WHERE u = 'https://…@h/x' OR v = 'https://…@h/x' OR w = 'https:…@h/x' "
                        "OR z = 'https://bob:pw' AND y = 'a@b';"), got
    # outside any run too: a stretch between runs, in a query's text and in a pass-through's
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, "u = 'x' OR https://bob:s3cr3t@h/x = [y]")]
    got = jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner)
    assert got == lines("SELECT a", "WHERE u = 'x' OR https://…@h/x = [y];"), got
    q = [row(TYPE, "EXEC p 'a', http://bob:s3cr3t@h/x", PASSTHROUGH, "ODBC;")]
    assert jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner) == "EXEC p 'a', http://…@h/x"
    q = [row(TYPE, None, SELECT), row(COLUMN, "'see //bob:pw@h/x'"), row(TABLE, None, None, "https://u:p.w@h/x"),
         row(WHERE, "a Like \"*key=1;*\"")]
    got = jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner)
    assert got == lines("SELECT 'see //…@h/x'", "FROM [https://…@h/x]", "WHERE a Like \"*key=1;*\";"), got
    q = [row(TYPE, "SELECT 1 FROM t WHERE u = '//bob:pw@h'\0", PASSTHROUGH, "ODBC;DSN=a")]
    assert jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner) == "SELECT 1 FROM t WHERE u = '//…@h'"
    # a tab or line break never ends an authority (a browser drops them before it reads a URL)
    w = "u = 'https://bob:s3cr3t\r\n@h/x' OR v = 'https://bob:s3\tcr3t@h/x'"
    q = [row(TYPE, None, SELECT), row(COLUMN, "a"), row(WHERE, w)]
    got = jet_sql.sql(q, connect=connect_cleaner, urls=url_cleaner)
    assert got == lines("SELECT a", "WHERE u = 'https://…@h/x' OR v = 'https://…@h/x';"), got


def test_unrebuildable():
    assert jet_sql.sql([row(TYPE, None, 99)]) is None  # an unknown type
    assert jet_sql.sql([]) is None and jet_sql.sql([row(COLUMN, "a")]) is None  # no type row
    assert jet_sql.sql([row(TYPE, None, SELECT)], flags=144) is None  # an object flag naming no type (dbQSPTBulk)
    rows = [row(TYPE, None, SELECT)] + [row(COLUMN, "c")] * (jet_sql.JET_QUERY_ROWS - 1)
    assert jet_sql.sql(rows) is not None
    assert jet_sql.sql(rows + [row(COLUMN, "x")]) is None  # 10,001 rows
    old = jet_sql.JET_QUERY_ROWS
    try:
        jet_sql.JET_QUERY_ROWS = 3  # read at call time
        assert jet_sql.sql(rows[:3]) is not None and jet_sql.sql(rows[:4]) is None
    finally:
        jet_sql.JET_QUERY_ROWS = old
    big = [row(TYPE, None, SELECT), row(COLUMN, "x" * (jet_sql.JET_SQL - len("SELECT ;")))]
    assert len(jet_sql.sql(big)) == jet_sql.JET_SQL
    big[1] = row(COLUMN, "x" * (jet_sql.JET_SQL - len("SELECT ;") + 1))
    assert jet_sql.sql(big) is None  # SQL past JET_SQL
    old = jet_sql.JET_SQL
    try:
        jet_sql.JET_SQL = 10
        assert jet_sql.sql([row(TYPE, None, SELECT), row(COLUMN, "ab")]) == "SELECT ab;"
        assert jet_sql.sql([row(TYPE, None, SELECT), row(COLUMN, "abc")]) is None
    finally:
        jet_sql.JET_SQL = old


def shapes():
    """The dearest shapes for `work`: many columns, long parameter names, joins, repeated conditions, deep joins,
    quoted runs, the Order sort, the scrub's IN clauses and prefixes."""
    n = 2000
    return {
        "columns": [row(TYPE, None, SELECT)] + [row(COLUMN, "a", None, "b c") for _ in range(n)],
        "params": [row(TYPE, None, SELECT)] + [row(PARAMETER, None, 4, "F!x" * 50) for _ in range(n)],
        "joins": [row(TYPE, None, SELECT)] + [row(TABLE, None, None, f"T{i}") for i in range(n)]
        + [row(JOIN, "c", 1, f"T{i}", f"T{i + 1}") for i in range(40)],
        "conditions": [row(TYPE, None, SELECT)] + [row(TABLE, None, None, f"T{i}") for i in range(200)]
        + [row(JOIN, "c", 1, "T0", "T1")] * n,
        "deep": [row(TYPE, None, SELECT)] + [row(TABLE, None, None, f"T{i}") for i in range(n)]
        + [row(JOIN, "c", 1, f"T{i}", f"T{i + 1}") for i in range(limits.MAX_DEPTH)]
        + [row(JOIN, "c", 1, "T0", "T1")] * n,
        "groups": [row(TYPE, None, SELECT), row(COLUMN, "'x;y=z'" * 4000)],
        "sorted": [Row(COLUMN, "a", None, None, None, 1, bytes([i % 256, i // 256])) for i in range(n)]
        + [Row(TYPE, None, SELECT, None, None, 1, b"\0")],
        "scrub": [row(TYPE, None, SELECT), row(WHERE, "x IN  '' [a;b].[c;d]. " * 2000 + "IN" + " " * 20_000)],
        "runs": [row(TYPE, None, SELECT), row(WHERE, "'a'[b]\"c\"[d;{e]}] .:/" * 3000 + "IN [x;")],
        "held": [row(TYPE, None, SELECT), row(WHERE, "'http:a'@b https:" * 4000 + "x [@" + "http://" * 2000)],
        "text": [row(TYPE, None, SELECT), row(COLUMN, "x" * 60000)],
    }


class Steps:
    """Counts what jet_sql.py runs while open (sys.monitoring): its backward jumps and its function starts."""
    TOOL = 5

    def __enter__(self):
        mon = sys.monitoring
        self.n = 0

        def event(code, *args):
            if code.co_filename != jet_sql.__file__:
                return mon.DISABLE
            if len(args) == 2 and args[1] >= args[0]:  # a forward jump
                return mon.DISABLE
            self.n += 1
        mon.use_tool_id(self.TOOL, "jet_sql steps")
        mon.register_callback(self.TOOL, mon.events.JUMP, event)
        mon.register_callback(self.TOOL, mon.events.PY_START, event)
        mon.set_events(self.TOOL, mon.events.JUMP | mon.events.PY_START)
        mon.restart_events()
        return self

    def __exit__(self, *exc):
        mon = sys.monitoring
        mon.set_events(self.TOOL, 0)
        mon.register_callback(self.TOOL, mon.events.JUMP, None)
        mon.register_callback(self.TOOL, mon.events.PY_START, None)
        mon.free_tool_id(self.TOOL)


def test_work_bounds_the_steps():
    """`work(rows)` is an upper bound on what sql() does, in steps; jet.py charges it before sql() runs. Here the
    loop trips and calls jet_sql.py makes stay within it on the dearest shapes (`measure` times them: each stays well
    under 0.2 microseconds a step, the meter's unit, which jet.py prices a step at)."""
    for name, rows in shapes().items():
        with Steps() as steps:
            jet_sql.sql(rows, connect=lambda s: s, urls=lambda s: s)
        assert steps.n <= jet_sql.work(rows), (name, steps.n, jet_sql.work(rows))


def measure():
    """Microseconds a step on `shapes()` (for jet.py's price)."""
    for name, rows in shapes().items():
        best = None
        for _ in range(3):
            t = time.perf_counter()
            jet_sql.sql(rows, connect=lambda s: s, urls=lambda s: s)
            dt = time.perf_counter() - t
            best = dt if best is None else min(best, dt)
        print(f"{name}: {best * 1e6 / jet_sql.work(rows):.3f} us a step ({jet_sql.work(rows)} steps, {best:.4f} s)")


def main():
    if sys.argv[1:] == ["measure"]:
        measure()
        return
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
