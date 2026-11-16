"""Access databases (mdb, accdb): the catalog, the user tables and the saved queries, as one summary line, a section
per table and a section per query.

Ported from Jackcess 5.0.4 (Apache-2.0; the Maven Central sources jar), `com/healthmarketscience/jackcess/impl/`:
`DatabaseImpl.java` (`getDatabasePassword` and `getPasswordMask`, the catalog's columns, object types and flags,
`collectRelationships`), `TableImpl.java` (the TDEF and its continuation pages, `readColumnDefinitions`,
`readIndexDefinitions`, the row offset table and its deleted and overflow bits, `positionAtRowData`,
`getRowColumnData`, `readJumpTableVarColOffsets`), `ColumnImpl.java` (the column block, its autonumber, hidden and
hyperlink flags, and `read`: currency, dates, extended dates, numerics and GUIDs), `MemoColumnImpl.java` (a hyperlink
is a Memo), `NumericColumnImpl.java` (precision and scale), `IndexData.java` and `IndexImpl.java` (the real and
logical index blocks, the primary key), `LongValueColumnImpl.java` (`readLongValue`: inline, one-row and chained long
values), `PropertyMaps.java` (the LvProp blob), `UsageMap.java` (the map row a TDEF points at), `NullMask.java` and
`JetFormat.java` (the offsets of each version), `ComplexColumnSupport.java` (MSysComplexColumns, the type tables'
names), `complex/ComplexColumnInfoImpl.java`, `complex/AttachmentColumnInfoImpl.java` (`decodeData`) and the other
`complex/` classes (the flat tables), and `CalculatedColumnUtil.java` (the wrapped value of a calculated column, its
Decimal form). mdbtools' `HACKING.md` (LGPL) was read as format knowledge only (the
ACE encryption structure whose length is at 0x299); no code is taken from it. The page layer is `jet_pages`.

Encryption. A password at 0x42 (20 bytes in Jet3; 40 in Jet4 and ACE, XOR the int part of the creation date at 0x72,
as `getPasswordMask` reads it) is `encrypted`, "database password set", although the pages are not encrypted. ACE with
a db_key or an encryption structure is `encrypted`, "ACE encryption". A Jet3 or Jet4 db_key is decoded by `jet_pages`.

Catalog. MSysObjects (the TDEF at page 2) gives `Obj` rows, at most `JET_CATALOG` (the rest counted `record`).
Objects flagged 0x80000000 or 0x2 are system objects, never shown or counted; 0x08 marks a hidden table. Text cells,
the catalog's included, go through `Jet.text` with the column's compressed-unicode flag (ext 0x01); a column name
is read once with its TDEF. A user table's LvProp definition (12 bytes, or the inline value) is kept with its row,
room allowing (past it the table is shown without properties, counted `record`).

Summary. "<version>: N tables, N queries, N linked tables.", then on its own line "Forms: … Reports: … Macros: …
Modules: … (names only)." (each part only when it has names; any module counts `vba`), then "Linked tables: <name> →
<target> (<table>)" joined with "; " (counted `linked_data`). A linked table's Connect, Database and ForeignName cells
are read as plain text (a hyperlink flag is not followed); the target is the connect string's DATABASE= part, else the
Database cell (an Access link keeps its path there), else the string itself, and PWD=, UID=, Password= and User ID=
parts (any case, a {braced} value whole) are dropped before anything is kept, so no credential is ever shown.

Properties. A table's LvProp blob (`KKD\\0` or `MR2\\0`, then chunks: the property names, the table's own map, a map
per column, index maps skipped) is read when the table is, once (`_props`), at most `JET_PROPS` bytes and
`JET_PROP_CHUNKS` chunks; past either, or malformed, the properties are ignored and counted `record`. Of a column's
properties, Format (only Percent and Yes/No are applied, to numbers: Percent is the shown digits x 100 with "%", Yes/No
"No" for 0, else "Yes"), TextFormat (1 is rich text: the text of its HTML, `html.text_of(html.tree(...))`, fed at most
`JET_CELL_CHARS` characters) and Required are used.

Relationships. MSysRelationships is read once, before any table: each row gives a foreign key (szObject, szColumn)
and what it names (szReferencedObject, szReferencedColumn); a row naming a table the catalog does not hold is left
out. The schema line shows the target after the column's type, "CustomerID (Long → Customers.ID)".

Schema line. "Columns: " then each column as "name (type[ → targets][, = expression][, PK][, autonumber][,
required][, rich text])"; an attachment column's type is "Attachment", another complex column's "Complex". A hidden
column (flag 0x10; a replication column, `s_GUID`, `s_Lineage`, `s_Generation` or `s_ColLineage`; or `Gen_X` where X
is a column of the same table; names without case) is a Hidden inline that holds its separator, and its grid column
is hidden. A user column that only looks like one (`s_total`) is shown.

Values. A Memo flagged 0x80 is a hyperlink, `display#address#subaddress#tip`: a Link when the address is http, https,
mailto or ftp and holds no space or control character, else "display (address)".

Calculated columns (ext 0xC0). The type is the ResultType property's (when it is a type read here), the value the
stored one: 16 bytes, a u32 length, the data (shorter than 20 bytes, as it is). A Yes/No is its first byte, not a
null-mask bit; text is in the compressed form whatever the ext byte; a Memo is a long value holding the wrapped data;
a Decimal is its own form (an i16 length, scale, sign, at most 16 bytes in a swapped order). The Expression shows in
the schema line on one line, cut at JET_CELL_CHARS.

Complex columns (type 18; Jackcess `impl/complex`). The column block holds a complex id at 11; its MSysComplexColumns
row (read once a document, its rows taking room) must name this table (ConceptualTableID) and, when it names one, this
column, and gives the flat table and the type table, whose name says the kind: `MSysComplexType_Attachment`, a
version history (`MSysComplexTypeVH_`) or a multi-value column (another `MSysComplexType_`); an unknown name leaves it
to the flat table's shape. The flat table (foreign key: ext 0x08; the autonumber key left out) is read once, its
values kept by key within their own room of four times the file's bytes (past it the rest are counted `record`). A
cell is its row's values (a version's columns joined with ", ") or file names, joined with "; ", cut at
JET_CELL_CHARS. A column that cannot be resolved shows empty cells, counted `record` once. An attachment that is a jpg,
jpeg, png or gif (by its name) is also an Image after the table, alt "<table> <key>: <file name>" (the key the row's
primary key values, else its row number), for the rows the grid shows: the wrapper (a u32 type, 0 raw or 1 deflated;
the content's length; the content, a header then the file) is read through `long_bytes`, and every picture's bytes,
raw or inflated (`flate.inflate`), come from one `flate.Budget(IMAGE_BYTES)` a document; past it, or a wrapper that
cannot be read (an unknown type, a cut stream, a header past the data), counts `image` and the file name stays.

Queries. After the tables, each saved query (catalog type 5, not a system object) is a section "Query: <name>"
(hidden when flagged 0x08) holding its SQL text in an `sql` Code block: its MSysQueries rows, grouped by ObjectId
in one walk of the table (`_query_rows`), rebuilt the way Jackcess writes them by `jet_sql` (never run). The connect
strings and database paths a query names, by position (`jet_sql`: a remote database, a source's qualifier, a
target, an IN clause's path and type and a [connect]. prefix in its text), are cleaned as a linked table's connect
string (`_clean_connect`), so no PWD=, UID= or URL user part shows there; any other literal or identifier is the
document's content, kept as stored, except that a URL's user part is taken out anywhere (`_clean_urls`). What is
withheld shows as "…", and each query showing one counts `withheld`. A pass-through query's stored text is shown as
stored (a URL's user part aside), its connect string never. A query whose rows are not kept (past
`jet_sql.JET_QUERY_ROWS`, a cell that cannot be read, a Memo past eight times `jet_sql.JET_SQL` bytes, the queries'
room of four times the file's bytes), whose SQL Jackcess cannot write or past `JET_SQL`, or past the meter keeps its
heading alone, counted `query`; a row of MSysQueries that cannot be read names no query, so then no query has SQL. A
control character other than a tab or line break shows as a space.

Page type 8. A page of type 8 in a table's usage map holds no rows read here: skipped, counted `jet_page8` (internal).

Key order. A table with a primary key shows its rows in key order: rows are held while they fit what the grid has
left (SHEET_ROWS - 1 rows, the cells and characters the shared budget still holds), then sorted by their typed key
(null first, numbers by value, text without case; equal keys keep their page order) when the meter affords the sort.
Past the grid, or past the meter, the rows are shown in page order: a sort never changes which rows are shown.

Rows. Each data page the table's usage map owns is read: a row flagged 0x8000 is deleted (skipped, not counted), one
flagged 0x4000 is a pointer followed once, to a row that must not be a pointer again; a table reads the row a
pointer names once and keeps only the values it showed (no bytes of it), so many pointers naming one row cost a
lookup each. Fixed columns are read at their `offset_F`, variable ones through their `offset_V` slot (Jet4's two-byte
offset table, Jet3's one-byte offsets and jump table), so rows written before a column was deleted read right. A
Yes/No value is its null-mask bit. Every offset and length is clipped to the row; a row or cell that cannot be read is
counted `record`. A row visits only the columns whose null-mask bits are set, and of those at most the row's own
column count (column numbers may repeat, so one bit can name many columns), each as a view into the row, never a
copy. Var slices are clipped but may overlap, so many cells can name the same bytes; the work meter (below) bounds
that.

Work meter. One meter per file (`jet_pages.Meter`, `limits.JET_WORK_TOTAL` or ten units a byte of the file and 1 MiB
if less, a unit about 0.2 microseconds) is the invariant: every loop whose trip count comes from the file's bytes
ticks it before its work, except two whose trips are grid cells, which `SHEET_CELLS` bounds: the Yes/No defaults a
row adds ("No" for each Yes/No column past its count) and `value()` on a cell that is shown (its `_CELL` given back).
Here: each page a usage map names (`_PAGE`), each row slot of a data page (`_SLOT`) and each live row (`_ROW`), each
null-mask byte a row looks at (`_GROUP`), each column a set bit names (`_CELL`), each Jet3 variable offset, each column
definition (`_COLDEF`) and its name's bytes, each index block (`_IDX`), each TDEF page, each text byte decoded, each
LVAL row (its bytes and `_ROW_COST`), each property chunk, name and value (`_PROP`), each character of rich text fed
to the HTML parser (`_HTML`) and of a hyperlink (`_LINK`), each key column of a held row (`_KEY`, and a unit a
character of a text key), the sort (`_SORT` times n times the bits of n, through `Meter.afford`: past what is left
the rows stay in page order) and each relationship target written (`_REF`); MSysRelationships', MSysComplexColumns'
and the flat tables' rows as any table's; the catalog looked through for the type tables (a unit an object); each
complex value joined (`_CPLX` and a unit a character), each attachment looked at for a picture (`_CPLX`) and each
picture read (`_ROW_COST`, its long value as any, and twice the most its inflation may produce, `_INFLATE` bytes a
unit, charged before it runs, kept when it fails; a definition decoded once); each connect string's characters and
marks (`_PART` a ";", "}" or quote); each name in the summary (`_NAME`); each character of a calculated column's
expression; MSysQueries' rows as any table's and `_QROW` each kept, each query (`_QUERY`) and its SQL, `_SQL` times
`jet_sql.work` (an upper bound on its steps) through `Meter.afford` before it is written (past what is left, no SQL).
In `jet_pages` each RC4 page, chain item and usage map byte. The prices are measured, each at least what its path
costs. A real file is charged a few units a byte of itself. Past the total: the table being read stops with one
`sheet_cut` (the grid's own cut, the rows held so far shown in page order); a table not yet started is not read at all
(not its TDEF, not its usage map) and counts `db_table`; in the catalog, and in MSysRelationships, the rest counts
`record` once.

Kept values. A long value read whole, an overflow target's shown values (and its sort key), a table's LvProp
definition and properties, the relationships' targets, linked tables' targets and MSysComplexColumns' rows are kept,
but only while their estimated size fits a room of the file's bytes (one room for all); a target that shows nothing
is not kept. The flat tables' values (and a picture's FileData definition) have their own room, four times the
file's bytes, and so do the queries' rows (a row 128 bytes and its strings), held until the queries are written.
Past the room a repeat is read again, charged like the first read (a definition or target past it is dropped,
counted `record`). So what is kept stays within about the file's size, the page cache within the decoded pages of
the file, and the rows held for the sort within the grid's caps.

Long values. A MEMO is inline (0x80), one LVAL row (0x40) or an LVAL chain (0x00). An LVAL value read whole is
decoded once and kept by its definition (room allowing), so a repeat costs its charge but no read; definitions that
differ but share a chain's tail are each charged, so the meter bounds them too. A chain that ends early (a cycle, an
empty row, the pages cap) shows what was read, counted `record` once.

Caps. `JET_COLS` columns per TDEF (past it a user table counts `db_table`, the catalog is `damaged`), `JET_NAME`
characters per name and `JET_CELL_CHARS` per cell (cut, counted `text`), `JET_LVAL_BYTES` read per long value,
`JET_PROPS` bytes and `JET_PROP_CHUNKS` chunks per property blob, `jet_sql.JET_QUERY_ROWS` rows and `jet_sql.JET_SQL`
characters per query, the work meter per file. Every cap is read at call
time, so a test can lower it.
"""
import collections
import copy
import datetime
import math
import re
import struct
import sys
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from operator import itemgetter
from pathlib import Path

from .. import html, limits
from ..containers import flate
from ..limits import ReadError
from ..model import Break, Code, Doc, Hidden, Image, Link, Para, Section, Text
from . import jet_pages, jet_sql, sheet_grid

JET_CATALOG = 100_000
JET_NAME = 64
JET_COLS = 1_024
JET_CELL_CHARS = 32_768
JET_LVAL_BYTES = 2 ** 20
JET_PROPS = 2 ** 20
JET_PROP_CHUNKS = 10_000

BOOL, BYTE, INT, LONG, MONEY, FLOAT, DOUBLE, DATE, BINARY, TEXT, OLE, MEMO = range(1, 13)
GUID, NUMERIC, BIGBIN, COMPLEX, BIGINT, EXTDATE = 15, 16, 17, 18, 19, 20
TYPE_NAMES = {BOOL: "Yes/No", BYTE: "Byte", INT: "Integer", LONG: "Long", MONEY: "Currency", FLOAT: "Single",
              DOUBLE: "Double", DATE: "Date", BINARY: "Binary", TEXT: "Text", OLE: "OLE", MEMO: "Memo", GUID: "GUID",
              NUMERIC: "Decimal", BIGBIN: "Big binary", COMPLEX: "Complex", BIGINT: "BigInt",
              EXTDATE: "Extended date"}
FIXED_SIZE = {BYTE: 1, INT: 2, LONG: 4, MONEY: 8, FLOAT: 4, DOUBLE: 8, DATE: 8, GUID: 16, NUMERIC: 17, COMPLEX: 4,
              BIGINT: 8, EXTDATE: 42}

VERSION_NAMES = {0: "Access 97 database (Jet 3)", 1: "Access 2000-2003 database (Jet 4)",
                 2: "Access 2007 database (ACE 12)", 3: "Access 2010 database (ACE 14)",
                 5: "Access 2016 database (ACE 16)", 6: "Access 2019 database (ACE 17)"}
TABLE, LINKED_ODBC, QUERY, LINKED = 1, 4, 5, 6
FORM, REPORT, MACRO, MODULE = -32768, -32764, -32766, -32761
SYSTEM_FLAGS = 0x80000002
HIDDEN_FLAG = 0x08

_DATA_PAGE, _TDEF_PAGE = 0x01, 0x02
_DELETED, _OVERFLOW, _OFFSET = 0x8000, 0x4000, 0x1FFF
_UNUSED = 0xFFFF
_ROW_COST = 256  # charged on top of each LVAL row's bytes, so a chain of tiny rows costs as its steps
# the meter's prices (units of about 0.2 microseconds, a byte of text decoded), each set from measurement at or above
# the dearest case of its path (the report's table): a cell that makes a grid cell is given back, the grid's caps
# bound those
_CELL = 8      # a column a set null-mask bit names (a var GUID counted `record` through a 1 KiB slot, about 6.4)
_ROW = 16      # a live row (an overflow pointer to an unreadable row, about 15)
_SLOT = 2      # a row slot of a data page, deleted or not
_GROUP = 4     # a null-mask byte a row looks at, its eight bits
_PAGE = 8      # a page a usage map names (or a TDEF page): its read and type check
_COLDEF = 32   # a column definition: its block, and its share of the schema line, header, groups and lookups
_IDX = 16      # a real or logical index block
_PROP = 4      # a property blob's chunk, name or value (a list of empty names, about 2.6)
_HTML = 8      # a character of rich text fed to the HTML parser (repeated <p>, about 5.2)
_LINK = 1      # a character of a hyperlink split and checked (about 0.25)
_KEY = 8       # a key column of a row kept for the primary-key sort (a one-column Long key, about 4.6)
_SORT = 4      # a step of that sort, n times the bits of n (ten Decimal columns, the last differing: about 2.2)
_REF = 8       # a relationship target written in the schema line (two names of up to JET_NAME, about 3.2)
_CPLX = 8      # a value of a complex cell joined, or a file looked at for a picture (and a unit a character joined)
_PART = 4      # a part of a linked table's connect string (and a unit a character)
_NAME = 8      # an object or linked table named in the summary (a name of up to JET_NAME, its target already charged)
_INFLATE = 64  # inflated bytes of an attachment a unit (charged before zlib's own loop, the most it may produce)
_KEEP = 256    # the room a kept value takes beside its text (key, tuples, a dict slot)
_QROW = 8      # an MSysQueries row of a query: its cells looked up, its `jet_sql.Row` made and kept
_QUERY = 16    # a query: its section, `jet_sql.work` over its rows, the cleaner's closure
_SQL = 1       # a step of `jet_sql.work` (the dearest shape measured, a parameter list, about 0.05 microseconds)
_Spent = jet_pages.Spent
_PK = 1  # IndexImpl.PRIMARY_KEY_INDEX_TYPE
_AUTONUMBER = 0x04 | 0x40  # ColumnImpl.AUTO_NUMBER_FLAG_MASK, AUTO_NUMBER_GUID_FLAG_MASK
_HIDDEN_COLUMN = 0x10      # ColumnImpl.HIDDEN_FLAG_MASK: a column Access maintains and hides (replication too)
_HYPERLINK = 0x80          # ColumnImpl.HYPERLINK_FLAG_MASK, on a Memo
_CALCULATED = 0xC0         # ColumnImpl.CALCULATED_EXT_FLAG_MASK (the ext byte)
_FOREIGN_KEY = 0x08        # the ext byte of a flat table's foreign key column (ComplexColumnInfoImpl)
_PAGE8 = 0x08              # a page type of ACE 14 and later that holds no rows this reader reads
# Access's replication columns (`s_Generation` and the like), hidden; `Gen_X` too when X is a column of its table
_REPLICATION = frozenset(("s_guid", "s_lineage", "s_generation", "s_collineage"))
_CALC_TYPES = frozenset((BOOL, BYTE, INT, LONG, MONEY, FLOAT, DOUBLE, DATE, TEXT, MEMO, GUID, NUMERIC, BIGINT))
# the connect string keys shown when it names no DATABASE= (every other key, a credential's too, is left out)
_SAFE_KEYS = frozenset(("dsn", "driver", "server", "dbq", "database", "data source", "provider", "host", "port", "app",
                        "trusted_connection"))
# a URL's authority as browsers read it (WHATWG): after a scheme and a run of "/" or "\" (either, or the run alone),
# up to the first "/", "\", "?" or "#"; a value with neither starts with its authority
_AUTHORITY = re.compile(r"((?:[A-Za-z][A-Za-z0-9+.-]*:)?[/\\]+)?([^/\\?#]*)")
_SEGMENTS = re.compile(r"[/\\?#]")
_PICTURES = frozenset(("jpg", "jpeg", "png", "gif"))
_PROP_HEADS = (b"KKD\x00", b"MR2\x00")  # JetFormat.PROPERTY_MAP_TYPES: Jet3, then Jet4 and ACE
_PROP_NAMES, _PROP_DEFAULT, _PROP_COLUMN, _PROP_INDEX = 0x80, 0x00, 0x01, 0x02  # PropertyMaps' chunk types
_WANTED = frozenset(("format", "description", "textformat", "expression", "required", "resulttype"))
LINK_SCHEMES = frozenset(("http", "https", "mailto", "ftp"))
_NUMBERS = frozenset((BYTE, INT, LONG, BIGINT, MONEY, FLOAT, DOUBLE, NUMERIC))
_ENCRYPTION_AT = 0x299  # the length of ACE's encryption structure (mdbtools HACKING.md; every real file read has 0)
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")
_SLASHES = re.compile(r"[/\\]+")
_WORD_STOP = re.compile(r"[\s'\"\[\]()<>,]")
_AUTH_STOP = re.compile(r"[/\\?#]")
_SQL_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")  # shown as a space in a query's text
_BAD = (ValueError, IndexError, struct.error, ArithmeticError)  # a row, cell or TDEF that cannot be read


class _Fmt:
    """The offsets of one version (Jackcess `JetFormat`): TDEF header, column and index blocks, data page."""

    def __init__(self, j3: bool):
        if j3:
            self.num_rows, self.num_cols, self.idx_slots, self.nidx, self.owned, self.idx_def = 12, 25, 27, 31, 35, 43
            self.idx_def_size, self.col_size, self.name_len = 8, 18, 1
            self.col_num, self.col_var, self.col_flags, self.col_ext = 1, 3, 13, None
            self.col_fixed, self.col_len = 14, 16
            self.real_block, self.real_skip, self.log_block, self.log_skip = 39, 0, 20, 0
            self.dp_rows, self.row_start = 8, 10
        else:
            self.num_rows, self.num_cols, self.idx_slots, self.nidx, self.owned, self.idx_def = 16, 45, 47, 51, 55, 63
            self.idx_def_size, self.col_size, self.name_len = 12, 25, 2
            self.col_num, self.col_var, self.col_flags, self.col_ext = 5, 7, 15, 16
            self.col_fixed, self.col_len = 21, 23
            self.real_block, self.real_skip, self.log_block, self.log_skip = 52, 4, 28, 4
            self.dp_rows, self.row_start = 12, 14
        self.precision, self.scale = 11, 12


@dataclass(slots=True)
class Obj:
    """One MSysObjects row. A linked table's `connect` is the target its connect string names (credentials removed,
    `_linked_target`), `foreign_name` the table there; both always strings."""
    id: int
    name: str
    type: int
    flags: int
    parent: int | None = None
    connect: str = ""
    foreign_name: str = ""
    lvprop: bytes = b""  # a user table's LvProp definition (12 bytes, or the inline value), room allowing


@dataclass
class Column:
    """One column block of a TDEF: `num` is the column number (the null-mask bit), `var` the variable offset slot
    (`offset_V`), `fixed_off` the fixed data offset (`offset_F`); flags 0x01 fixed, 0x04 and 0x40 autonumber, 0x10
    hidden, 0x80 hyperlink; ext 0x01 compressed unicode, 0xC0 calculated. `complex_id` is a complex column's id (at
    11 of the block). `hidden` (`_tdef`): flagged 0x10, a replication column or a `Gen_` one naming a column of the
    table. `fmt` (the Format property, lower case), `rich` (TextFormat 1), `required` and, of a calculated column,
    `expr` (Expression) and its type (ResultType, which replaces the block's) come from the table's properties."""
    name: str
    type: int
    num: int
    var: int = 0
    fixed_off: int = 0
    length: int = 0
    flags: int = 0x01
    ext: int = 0
    precision: int = 0
    scale: int = 0
    fmt: str = ""
    rich: bool = False
    required: bool = False
    complex_id: int = 0
    hidden: bool = False
    expr: str = ""


@dataclass
class Tdef:
    """A table definition: its page chain, the row count it records, its columns in column-number order, the real
    indexes' column numbers, the logical indexes as (index type, real index), the primary key's column numbers and the
    owned-pages usage map."""
    page: int
    pages: list
    row_count: int
    columns: list
    real: list = field(default_factory=list)
    logical: list = field(default_factory=list)
    pk: list = field(default_factory=list)
    owned: bytes = b""
    groups: list = field(default_factory=list)  # (null-mask byte, [the (index, Column)s of each of its 8 bits])


def _u16(b, at):
    return struct.unpack_from("<H", b, at)[0]


def _u32(b, at):
    return struct.unpack_from("<I", b, at)[0]


def _need(raw: bytes, n: int) -> bytes:
    if len(raw) != n:
        raise ValueError(f"{len(raw)} bytes where {n} were expected")
    return raw


def _java_int(d: float) -> int:
    """Java's `(int)` of a double: NaN is 0, the rest truncated toward zero and saturated."""
    if d != d:
        return 0
    if d >= 2 ** 31 - 1:
        return 2 ** 31 - 1
    if d <= -2 ** 31:
        return -2 ** 31
    return int(d)


class _Db:
    """One open database: the page layer, the parse context and the version's offsets."""

    def __init__(self, jet, ctx):
        self.jet, self.ctx = jet, ctx
        self.j3 = jet.version == 0
        self.f = _Fmt(self.j3)
        self.tdef_pages = set()  # continuation pages already part of a TDEF: a chain reaching one ends there
        self.meter = jet.meter
        self.charge = jet.meter.tick
        self.lval_cache = {}  # (definition, compressed) -> (text, bytes read, charge) of a long value read whole
        self.props_cache = {}  # object id -> its properties (`_props`), room allowing
        # bytes the kept values (long values, overflow targets, LvProp definitions and properties, relationship
        # targets, linked tables' targets, MSysComplexColumns' rows and the flat tables' values) may take
        self.room = jet.pages * jet.page_size
        # bytes the flat tables' values may take: a short value's str is up to about four times its row (Python's
        # str head against a row of a few bytes), so four times the file
        self.flat_room = 4 * self.room
        self.images = flate.Budget(limits.IMAGE_BYTES)  # every attachment's picture bytes, one budget a document
        self.cplx = None  # MSysComplexColumns (`_complex_columns`), read once when a complex column is first met
        self.cplx_info = {}  # complex id -> its `_Complex` (False when it cannot be read)
        self.pictures = {}  # an attachment's FileData definition -> its picture's bytes, or None (`_picture`)

    @property
    def spent(self) -> int:
        return self.meter.spent

    @property
    def total(self) -> int:
        return self.meter.total

    def keep(self, size: int) -> bool:
        """Take `size` bytes (and `_KEEP`) of the room for a kept value; False, taking nothing, past it."""
        if size + _KEEP > self.room:
            return False
        self.room -= size + _KEEP
        return True

    def keep_flat(self, size: int) -> bool:
        """Take `size` bytes of the flat tables' room (`flat_room`); False, taking nothing, past it."""
        if size > self.flat_room:
            return False
        self.flat_room -= size
        return True

    # -- pages and rows

    def rows_on(self, page: bytes) -> int:
        """The row count of a data page, at most what its row offset table can hold."""
        return min(_u16(page, self.f.dp_rows), (len(page) - self.f.row_start) // 2)

    def span(self, page: bytes, r: int):
        """(start, end, flags) of row `r`: it ends where row r - 1 starts, row 0 at the page end."""
        at = self.f.row_start + 2 * r
        if r < 0 or at + 2 > len(page):
            raise ValueError(f"row {r} outside its page")
        loc = _u16(page, at)
        start = loc & _OFFSET
        end = len(page) if r == 0 else _u16(page, at - 2) & _OFFSET
        if not start < end <= len(page):
            raise ValueError(f"row {r} spans {start}..{end}")
        return start, end, loc & (_DELETED | _OVERFLOW)

    def row_at(self, pg: int, r: int) -> bytes:
        """The bytes of row `r` of data page `pg`, whatever its flags (an LVAL row, an overflow target, a map)."""
        page = self.jet.page(pg)
        if page[0] != _DATA_PAGE or r >= self.rows_on(page):
            raise ValueError(f"no row {r} on page {pg}")
        start, end, _ = self.span(page, r)
        return page[start:end]

    def row_data(self, page: bytes, r: int):
        """(bytes, None) for a live row; (None, (page, row)) for an overflow row, the row its pointer names."""
        start, end, flags = self.span(page, r)
        if not flags & _OVERFLOW:
            return page[start:end], None
        if end - start < 4:
            raise ValueError("overflow row shorter than its pointer")
        return None, (int.from_bytes(page[start + 1:start + 4], "little"), page[start])

    def target(self, tp: int, tr: int) -> bytes:
        """The bytes of the row an overflow pointer names, which must not be a pointer again."""
        target = self.jet.page(tp)
        if target[0] != _DATA_PAGE or tr >= self.rows_on(target):
            raise ValueError(f"overflow row points at no row ({tp}, {tr})")
        start, end, flags = self.span(target, tr)
        if flags & _OVERFLOW:
            raise ValueError("overflow row points at another overflow row")
        return target[start:end]

    def map_row(self, ptr: bytes) -> bytes:
        """The usage map row a TDEF's four-byte pointer (row, then a three-byte page) names."""
        r, pg = ptr[0], int.from_bytes(ptr[1:4], "little")
        if pg <= 0:
            raise ValueError(f"usage map on page {pg}")
        page = self.jet.page(pg)
        start, end, _ = self.span(page, r)
        if end - start < 5:
            raise ValueError("usage map row too short")
        return page[start:end]

    # -- text

    def name(self, s: str) -> str:
        """A table or column name on one line (control characters to spaces), cut at JET_NAME, counted `text`."""
        s = _CONTROL.sub(" ", s).strip()
        if len(s) > JET_NAME:
            s = s[:JET_NAME]
            self.ctx.skipped["text"] += 1
        return s

    def cut(self, s: str, more: bool = False, rich: bool = False) -> str:
        """A cell's text, at most JET_CELL_CHARS characters; a cut (or a long value read in part) ends with "…" and is
        counted `text` once. Rich text is the text of its HTML (`rich_text`), the cut decided on the stored text."""
        over = len(s) > JET_CELL_CHARS or more
        if rich:
            s = self.rich_text(s)
        if over:
            self.ctx.skipped["text"] += 1
            return s[:JET_CELL_CHARS] + "…"
        return s

    def rich_text(self, s: str) -> str:
        """The text of a rich text value's HTML (`html.text_of(html.tree(...))`), fed at most JET_CELL_CHARS
        characters, each charged `_HTML` first."""
        s = s[:JET_CELL_CHARS]
        self.charge(_HTML * len(s))
        return html.text_of(html.tree(s.encode("utf-8")))

    def decode(self, raw, compressed: bool) -> str:
        """A cell's text bytes decoded, charged their length first."""
        self.charge(len(raw))
        return self.jet.text(raw, compressed)

    # -- long values

    def memo(self, raw: bytes, compressed: bool, rich: bool = False) -> str:
        """A MEMO cell's text: inline (0x80), one LVAL row (0x40) or an LVAL chain (0x00), read up to JET_LVAL_BYTES
        and no further than JET_CELL_CHARS characters can take, each charged against the file's work total. A value
        shorter than its length is counted `record` (and only that, however long its text); a cut at a cap ends with
        "…", counted `text`. With `rich`, the text of its HTML."""
        if len(raw) < 12:
            raise ValueError("long value definition shorter than 12 bytes")
        head = _u32(raw, 0)
        length, kind = head & 0x3FFFFFFF, head >> 30
        # two bytes a character, twice that for mode switches, FF FE
        want = min(length, JET_LVAL_BYTES, 4 * JET_CELL_CHARS + 4)
        if kind == 2:
            data = raw[12:12 + want]
            return self._whole(self.decode(data, compressed), len(data), want, length, rich)
        if kind == 3:
            raise ValueError("long value type 0xC0")
        key = (bytes(raw[:12]), compressed)
        hit = self.lval_cache.get(key)
        if hit is not None:
            s, n, charge = hit
            self.charge(charge)
            return self._whole(s, n, want, length, rich)
        before = self.spent
        data = self._lval((int.from_bytes(raw[5:8], "little"), raw[4]), want, kind == 1)
        s = self.jet.text(data, compressed)
        if self.keep(sys.getsizeof(s) + len(key[0])):
            self.lval_cache[key] = (s, len(data), self.spent - before)
        return self._whole(s, len(data), want, length, rich)

    def _whole(self, s: str, n: int, want: int, length: int, rich: bool) -> str:
        if n < want:  # counted once, `record`: a cut of its text is not counted again
            self.ctx.skipped["record"] += 1
            over = len(s) > JET_CELL_CHARS
            if rich:
                s = self.rich_text(s)
            return s[:JET_CELL_CHARS] + "…" if over else s
        return self.cut(s, length > want, rich)

    def long_bytes(self, raw, cap: int):
        """The bytes of a long value (an LvProp blob) read whole, or None when its length is past `cap` (nothing is
        read). ValueError when its rows hold less than its length."""
        if len(raw) < 12:
            raise ValueError("long value definition shorter than 12 bytes")
        head = _u32(raw, 0)
        length, kind = head & 0x3FFFFFFF, head >> 30
        if length > cap:
            return None
        if kind == 3:
            raise ValueError("long value type 0xC0")
        if kind == 2:
            data = bytes(raw[12:12 + length])
        else:
            data = self._lval((int.from_bytes(raw[5:8], "little"), raw[4]), length, kind == 1)
        if len(data) < length:
            raise ValueError("long value shorter than its length")
        return data

    def props_of(self, obj: "Obj") -> dict:
        """The properties of a table (`_props` of its LvProp blob), read once per object and kept while the room
        lasts. A blob past JET_PROPS bytes or JET_PROP_CHUNKS chunks, or one that cannot be read, is ignored and
        counted `record`."""
        hit = self.props_cache.get(obj.id)
        if hit is not None:
            return hit
        out = {}
        if obj.lvprop:
            try:
                blob = self.long_bytes(obj.lvprop, JET_PROPS)
                if blob is None:
                    self.ctx.skipped["record"] += 1
                else:
                    out = _props(self, blob)
            except _BAD:
                self.ctx.skipped["record"] += 1
                out = {}
        size = sum(64 + sum(64 + sys.getsizeof(v) for v in p.values()) for p in out.values())
        if self.keep(size):
            self.props_cache[obj.id] = out
        return out

    def _lval(self, start, want: int, single: bool):
        """The bytes of one LVAL row or chain, each row charged as it is read. A chain row starts with the (row,
        three-byte page) of the next, 0 ending it; `Jet.chain` keeps the seen-set, so a loop ends the value, and an
        empty chunk ends it too."""
        out = bytearray()
        rows = {}

        def step(item):
            b = rows.pop(item)
            nxt = int.from_bytes(b[1:4], "little")
            return (nxt, b[0]) if nxt else None

        for item in [start] if single else self.jet.chain(start, step):
            self.charge(_ROW_COST)
            b = self.row_at(item[0], item[1])
            self.charge(len(b))
            if single:
                return b[:want]
            if len(b) <= 4:
                break
            rows[item] = b
            out += b[4:4 + want - len(out)]
            if len(out) >= want:
                break
        return bytes(out)


# -- encryption

def _refuse_encrypted(jet):
    page0 = jet.page(0)
    if jet.version >= 2 and (jet.db_key or _u16(page0, _ENCRYPTION_AT)):
        raise ReadError("encrypted", "ACE encryption")
    pw = page0[0x42:0x42 + (20 if jet.version == 0 else 40)]
    if jet.version:
        mask = _java_int(struct.unpack_from("<d", page0, 0x72)[0]).to_bytes(4, "little", signed=True)
        pw = bytes(b ^ mask[i % 4] for i, b in enumerate(pw))
    if any(pw):
        raise ReadError("encrypted", "database password set")


# -- table definitions

def _tdef(db: _Db, page: int) -> Tdef:
    """The TDEF at `page` with its continuation pages. Past JET_COLS columns it is refused (ValueError, so the caller
    counts it)."""
    jet, f = db.jet, db.f
    first = jet.page(page)
    if first[0] != _TDEF_PAGE:
        raise ValueError(f"page {page} is not a table definition")
    pages, parts = [page], [first]
    for pg in jet.chain(page, jet.next_pg):
        if pg == page:
            continue
        db.charge(_PAGE)
        p = jet.page(pg)
        if p[0] != _TDEF_PAGE or pg in db.tdef_pages:  # no two tables share a page, so the work stays linear
            break
        db.tdef_pages.add(pg)
        pages.append(pg)
        parts.append(p[8:])
    b = b"".join(parts)
    row_count = _u32(b, f.num_rows)
    ncols = _u16(b, f.num_cols)
    if ncols > JET_COLS:
        raise ValueError(f"{ncols} columns, past JET_COLS")
    nlog, nidx = _u32(b, f.idx_slots), _u32(b, f.nidx)
    col_at = f.idx_def + nidx * f.idx_def_size
    pos = col_at + ncols * f.col_size
    if pos > len(b):
        raise ValueError("column blocks past the table definition")
    db.charge(_COLDEF * ncols)
    names = []
    for _ in range(ncols):
        n = b[pos] if f.name_len == 1 else _u16(b, pos)
        pos += f.name_len
        if pos + n > len(b):
            raise ValueError("column name past the table definition")
        db.charge(n)
        names.append(db.name(jet.text(b[pos:pos + n], None)))
        pos += n
    cols = []
    folded = {n.casefold() for n in names}
    for i, name in enumerate(names):
        o = col_at + i * f.col_size
        c = Column(name, b[o], _u16(b, o + f.col_num), _u16(b, o + f.col_var), _u16(b, o + f.col_fixed),
                   _u16(b, o + f.col_len), b[o + f.col_flags], b[o + f.col_ext] if f.col_ext else 0,
                   b[o + f.precision], b[o + f.scale], complex_id=0 if db.j3 else _u32(b, o + f.precision))
        n = name.casefold()
        c.hidden = bool(c.flags & _HIDDEN_COLUMN) or n in _REPLICATION or (n.startswith("gen_") and n[4:] in folded)
        cols.append(c)
    cols.sort(key=lambda c: c.num)
    if pos + nidx * f.real_block > len(b):
        raise ValueError("index block past the table definition")
    db.charge(_IDX * nidx)
    real = []
    for _ in range(nidx):
        if pos + f.real_block > len(b):
            raise ValueError("index block past the table definition")
        o = pos + f.real_skip
        real.append([n for n in (_u16(b, o + 3 * k) for k in range(10)) if n != _UNUSED])
        pos += f.real_block
    if pos + nlog * f.log_block > len(b):
        raise ValueError("logical index block past the table definition")
    db.charge(_IDX * nlog)
    logical = []
    for _ in range(nlog):
        if pos + f.log_block > len(b):
            raise ValueError("logical index block past the table definition")
        o = pos + f.log_skip
        logical.append((b[o + 19], _u32(b, o + 4)))
        pos += f.log_block
    pk = next((real[d] for kind, d in logical if kind == _PK and d < len(real)), [])
    owned = db.map_row(b[f.owned:f.owned + 4])
    groups = {}
    for i, c in enumerate(cols):
        groups.setdefault(c.num >> 3, [[] for _ in range(8)])[c.num & 7].append((i, c))
    return Tdef(page, pages, row_count, cols, real, logical, pk, owned, sorted(groups.items()))


# -- properties

def _props(db: _Db, blob: bytes) -> dict:
    """An LvProp blob (`PropertyMaps.Handler.read`): `KKD\\0` (Jet3) or `MR2\\0`, then chunks of a u32 length and a
    u16 type: 0x80 the property names (each a u16 length and its bytes), 0x00 the object's own map, 0x01 a column's
    map, 0x02 an index's (skipped). A map is a u32 name block (a name when longer than 6), then values: u16 length,
    flags, data type, u16 name index, u16 data size and the data. Returns {map name, lower case ("" for the object's
    own): {property, lower case: value}} for Format, Description, TextFormat, Expression and Required. Each chunk,
    name and value is charged `_PROP` and each text byte decoded 1 before it is read. A malformed blob, or one past
    JET_PROP_CHUNKS chunks, raises ValueError."""
    if bytes(blob[:4]) not in _PROP_HEADS:
        raise ValueError("not a property blob")
    end, pos, chunks, names, out = len(blob), 4, 0, None, {}
    while pos < end:
        chunks += 1
        if chunks > JET_PROP_CHUNKS:
            raise ValueError("past JET_PROP_CHUNKS")
        db.charge(_PROP)
        size, kind = struct.unpack_from("<IH", blob, pos)
        stop = pos + size
        if size < 6 or stop > end:
            raise ValueError(f"property chunk of {size} bytes at {pos}")
        if kind == _PROP_NAMES:
            names = _prop_names(db, blob, pos + 6, stop)
        elif kind in (_PROP_DEFAULT, _PROP_COLUMN):
            name, values = _prop_values(db, blob, pos + 6, stop, names)
            out.setdefault("" if kind == _PROP_DEFAULT else name.lower(), {}).update(values)
        elif kind != _PROP_INDEX:
            raise ValueError(f"property chunk type {kind:#x}")
        pos = stop
    return out


def _prop_name(db: _Db, blob: bytes, pos: int, stop: int):
    """(name, the position after it): a u16 length and its bytes, uncompressed text."""
    if pos + 2 > stop:
        raise ValueError("property name length past its chunk")
    n = struct.unpack_from("<h", blob, pos)[0]
    pos += 2
    if n < 0 or pos + n > stop:
        raise ValueError(f"property name of {n} bytes")
    db.charge(n)
    return db.jet.text(blob[pos:pos + n], None), pos + n


def _prop_names(db: _Db, blob: bytes, pos: int, stop: int) -> list:
    names = []
    while pos < stop:
        db.charge(_PROP)
        name, pos = _prop_name(db, blob, pos, stop)
        names.append(name.lower())
    return names


def _prop_values(db: _Db, blob: bytes, pos: int, stop: int, names):
    """(map name, {property: value}) of one map chunk; only the wanted properties are decoded."""
    name = ""
    if pos < stop:
        if pos + 4 > stop:
            raise ValueError("map name block past its chunk")
        n = _u32(blob, pos)
        after = pos + n
        if n < 4 or after > stop:
            raise ValueError(f"map name block of {n} bytes")
        if n > 6:
            name = db.name(_prop_name(db, blob, pos + 4, after)[0])
        pos = after
    values = {}
    while pos < stop:
        db.charge(_PROP)
        if pos + 8 > stop:
            raise ValueError("property value header past its chunk")
        size, _, dtype, idx, n = struct.unpack_from("<hBBhh", blob, pos)
        if size < 8 or pos + size > stop:
            raise ValueError(f"property value of {size} bytes")
        if names is None or not 0 <= idx < len(names):
            raise ValueError(f"property name index {idx}")
        if n < 0 or pos + 8 + n > stop:
            raise ValueError(f"property data of {n} bytes")
        key = names[idx]
        if key in _WANTED:
            values[key] = _prop_value(db, dtype, blob[pos + 8:pos + 8 + n])
        pos += size
    return name, values


def _prop_value(db: _Db, dtype: int, data: bytes):
    """Text (Text, Memo) through the file's text form, a number (Byte, Integer, Long), Yes/No as a bool; else None."""
    if dtype in (TEXT, MEMO):
        db.charge(len(data))
        return db.jet.text(data, True)
    if dtype == BOOL:
        if not data:
            raise ValueError("an empty Yes/No property")
        return data[0] != 0
    if dtype in (BYTE, INT, LONG):
        size = {BYTE: 1, INT: 2, LONG: 4}[dtype]
        if len(data) < size:  # too few for its type (PropertyMaps refuses the blob)
            raise ValueError(f"{len(data)} bytes for a {size}-byte property")
        return int.from_bytes(data[:size], "little", signed=dtype != BYTE)
    return None


def _apply_props(db: _Db, tdef: Tdef, props: dict):
    """Format (lower case), TextFormat 1 (rich text, on Text and Memo) and Required onto the table's columns; of a
    calculated column, its Expression and its ResultType (a type this reader reads), which is its type from here
    (`ColumnImpl.create`)."""
    for c in tdef.columns:
        p = props.get(c.name.lower())
        if not p:
            continue
        if c.ext & _CALCULATED:
            result, expr = p.get("resulttype"), p.get("expression")
            if type(result) is int and result in _CALC_TYPES:
                c.type = result
            c.expr = expr if isinstance(expr, str) else ""
        fmt = p.get("format")
        c.fmt = fmt.strip().lower() if isinstance(fmt, str) else ""
        c.rich = p.get("textformat") == 1 and c.type in (TEXT, MEMO)
        c.required = bool(p.get("required"))


# -- relationships

def _relationships(db: _Db, objs: list) -> dict:
    """{(table, column), lower case: {"Table.Column": None}} from MSysRelationships (Jackcess
    `DatabaseImpl.collectRelationships`: szObject and szColumn hold the foreign key, szReferencedObject and
    szReferencedColumn what it names). A row naming a table the catalog does not hold is left out. Its rows and text
    are charged as any table's; a target is kept once, while the room lasts (past it the rest are counted `record`
    once). The meter running out, or a table that cannot be read, ends it: counted `record` once."""
    if db.meter.over:  # the catalog was cut, and counted
        return {}
    tables = {o.name.lower() for o in objs if o.type == TABLE and not o.flags & SYSTEM_FLAGS}
    rel = next((o for o in objs if o.type == TABLE and o.flags & SYSTEM_FLAGS
                and o.name.lower() == "msysrelationships"), None)
    out = {}
    if rel is None:
        return out
    roomless = False
    try:
        tdef = _tdef(db, rel.id & 0xFFFFFF)
        at = {c.name.lower(): i for i, c in reversed(list(enumerate(tdef.columns)))}
        want = ("szobject", "szcolumn", "szreferencedobject", "szreferencedcolumn")
        if not all(k in at for k in want):
            raise ValueError("MSysRelationships has no szObject, szColumn or szReferenced columns")
        cols = [tdef.columns[at[k]] for k in want]
        for _, cells in _rows(db, tdef):
            try:
                t, c, rt, rc = (_name_cell(db, col, cells.get(at[k])) for col, k in zip(cols, want))
            except _BAD:
                db.ctx.skipped["record"] += 1
                continue
            if not (t and c and rt and rc) or t.lower() not in tables or rt.lower() not in tables:
                continue
            target = f"{rt}.{rc}"
            seen = out.get((t.lower(), c.lower()))
            if seen is not None and target in seen:
                continue
            if not db.keep(sys.getsizeof(target) + (0 if seen is not None else 128)):
                if not roomless:
                    roomless = True
                    db.ctx.skipped["record"] += 1
                continue
            out.setdefault((t.lower(), c.lower()), {})[target] = None
    except (_Spent, *_BAD):
        db.ctx.skipped["record"] += 1
    return out


def _name_cell(db: _Db, col: Column, raw):
    """A Text or Memo cell holding a name, on one line and cut at JET_NAME; None when null or of another type."""
    if raw is None or col.type not in (TEXT, MEMO):
        return None
    return db.name(_plain_text(db, col, raw))


def _plain_text(db: _Db, col: Column, raw) -> str:
    """A Text or Memo cell's text whatever its flags (a hyperlink flag is not followed), cut at JET_CELL_CHARS; "" when
    null or of another type."""
    if raw is None or col.type not in (TEXT, MEMO):
        return ""
    if col.type == TEXT:
        return db.cut(db.decode(raw, bool(col.ext & 0x01)))
    return db.memo(raw, bool(col.ext & 0x01))


# -- complex columns (attachments, multi-value, version history)

def _long(col: Column, raw):
    """A Long cell's value; None when null."""
    if raw is None:
        return None
    if col.type != LONG:
        raise ValueError(f"type {col.type} where a Long was expected")
    return struct.unpack("<i", raw)[0]


def _complex_kind(name):
    """The kind of complex column a type table's name says (`ComplexColumnSupport.create`): "attachment", "version",
    "multi", "" for a name it does not know (the flat table's shape decides) or None when there is no such table.
    Jackcess judges by the type table's columns (`isAttachmentColumn`, `isVersionHistoryColumn`); the name is this
    reader's departure, the shape in `_flat` its fallback."""
    if name is None:
        return None
    n = name.casefold()
    if n == "msyscomplextype_attachment":
        return "attachment"
    if n.startswith("msyscomplextypevh_"):
        return "version"
    return "multi" if n.startswith("msyscomplextype_") else ""


def _complex_columns(db: _Db, objs: list) -> dict:
    """MSysComplexColumns as {complex id: [column name or None, kind (`_complex_kind` of the type table), flat table
    page, table page]}, the first row of an id kept. Its rows are charged as any table's; each row kept takes room
    (past it the rest are counted `record` once), and the catalog is looked through once (a unit an object) for the
    type tables' names. A table that cannot be read is counted `record` once, keeping the rows read; the meter running
    out raises `Spent` for the caller."""
    t = next((o for o in objs if o.type == TABLE and o.flags & SYSTEM_FLAGS
              and o.name.lower() == "msyscomplexcolumns"), None)
    out = {}
    if t is None:
        return out
    try:
        tdef = _tdef(db, t.id & 0xFFFFFF)
        at = {c.name.lower(): i for i, c in reversed(list(enumerate(tdef.columns)))}
        want = ("columnname", "complextypeobjectid", "flattableid", "conceptualtableid", "complexid")
        if not all(k in at for k in want):
            raise ValueError("MSysComplexColumns lacks a column")
        cols = [tdef.columns[at[k]] for k in want]
        for _, cells in _rows(db, tdef):
            try:
                name = _name_cell(db, cols[0], cells.get(at[want[0]]))
                kind, flat, table, cid = (_long(c, cells.get(at[k])) for c, k in zip(cols[1:], want[1:]))
            except _BAD:
                db.ctx.skipped["record"] += 1
                continue
            if None in (kind, flat, table, cid) or cid in out:
                continue
            if not db.keep(64 + sys.getsizeof(name)):
                db.ctx.skipped["record"] += 1
                break
            out[cid] = [name, kind, flat, table]
    except _BAD:
        db.ctx.skipped["record"] += 1
    pages = {r[1] for r in out.values()}
    db.charge(len(objs))
    names = {}
    for o in objs:
        if o.type == TABLE and o.id & 0xFFFFFF in pages:
            names.setdefault(o.id & 0xFFFFFF, o.name)
    for r in out.values():
        r[1] = _complex_kind(names.get(r[1]))
    return out


class _Files(str):
    """An attachment cell's text, the file names; `fk` is its row's key into the flat table, `info` its column's."""


class _Complex:
    """A complex column's flat table read once: `kind` ("attachment", "multi" or "version") and `rows`, {foreign key:
    [entries]}: an attachment's (file name, its FileData definition when a picture, else None), another kind's text."""

    def __init__(self, kind: str, rows: dict):
        self.kind, self.rows = kind, rows

    def cell(self, db: _Db, raw) -> str:
        """The values (file names) of a row's cell joined with "; ", each charged `_CPLX` and its characters before it
        is added; past JET_CELL_CHARS the join stops, cut with "…" and counted `text`."""
        fk = struct.unpack("<i", raw)[0]
        parts, size = [], -2
        for e in self.rows.get(fk, ()):
            s = e[0] if self.kind == "attachment" else e
            db.charge(_CPLX + len(s))
            if not s:
                continue
            parts.append(s)
            size += len(s) + 2
            if size > JET_CELL_CHARS:
                break
        text = db.cut("; ".join(parts), more=size > JET_CELL_CHARS)
        if self.kind != "attachment":
            return text
        out = _Files(text)
        out.fk, out.info = fk, self
        return out

    def pictures(self, db: _Db, fk: int, alt: str, hidden: bool, out=None) -> list:
        """The pictures (jpg, png, gif) of a row's attachments as Images, alt "<alt>: <file name>", appended to `out`
        one by one (so those made stay when the meter runs out on the next): each file charged `_CPLX` before it is
        looked at, each picture `_ROW_COST` before it is read (`_picture`)."""
        out = [] if out is None else out
        for name, definition in self.rows.get(fk, ()):
            db.charge(_CPLX)
            if definition is None:
                continue
            db.charge(_ROW_COST)
            data = _picture(db, definition)
            if data:
                out.append(Image(data, f"{alt}: {name}", hidden="hidden" if hidden else ""))
        return out


def _complex_info(db: _Db, objs: list, tdef: Tdef, col: Column):
    """The `_Complex` of a complex column (`ComplexColumnSupport.create`): its MSysComplexColumns row (by the complex
    id) must name this table and, when it names one, this column; its flat table is read once a document. None,
    counted `record`, when it cannot be resolved or read."""
    if db.cplx is None:
        db.cplx = _complex_columns(db, objs)
    row = db.cplx.get(col.complex_id)
    if row is None or row[3] != tdef.page or row[1] is None or (row[0] and row[0].casefold() != col.name.casefold()):
        db.ctx.skipped["record"] += 1
        return None
    info = db.cplx_info.get(col.complex_id)
    if info is None:
        try:
            info = _flat(db, row[1], row[2])
        except _BAD:
            info = None
        db.cplx_info[col.complex_id] = info or False
    if not info:
        db.ctx.skipped["record"] += 1
        return None
    return info


def _flat(db: _Db, kind: str, page: int):
    """A complex column's flat table (`ComplexColumnInfoImpl`): the foreign key column (ext 0x08), the autonumber key
    (left out) and the type's columns. A kind the name did not give is taken from the shape: FileData and FileName
    make an attachment, one value column a multi-value column, more a version history. Each row is read as any
    table's (charged so); each entry kept takes its size of the flat room (`_Db.keep_flat`), past it the rest are
    counted `record` once. None when the table has no foreign key (or an attachment table no FileData or FileName)."""
    ft = _tdef(db, page)
    cols = ft.columns
    fk = next((i for i, c in enumerate(cols) if c.ext & _FOREIGN_KEY and c.type == LONG), None)
    if fk is None:
        return None
    named = {c.name.casefold(): i for i, c in enumerate(cols)}
    vals = {i for i, c in enumerate(cols) if i != fk and not c.flags & _AUTONUMBER}
    if not kind:
        kind = "attachment" if "filedata" in named and "filename" in named else "multi" if len(vals) == 1 else "version"
    if kind == "attachment" and not ("filedata" in named and "filename" in named):
        return None
    iname, idata = named.get("filename"), named.get("filedata")
    rows = {}
    for _, cells in _rows(db, ft):
        try:
            key = _long(cols[fk], cells.get(fk))
            if key is None:
                continue
            if kind == "attachment":
                name = _CONTROL.sub(" ", _plain_text(db, cols[iname], cells.get(iname))).strip()
                raw = cells.get(idata)
                picture = name.rpartition(".")[2].casefold() in _PICTURES and "." in name and raw is not None
                entry = (name, bytes(raw) if picture else None)
                size = sys.getsizeof(name) + (len(entry[1]) if picture else 0)
            else:
                texts = [v for v in (value(db, cols[i], raw) for i, raw in cells.items() if i in vals)
                         if isinstance(v, str) and v]
                entry = ", ".join(texts)
                size = sys.getsizeof(entry)
        except _BAD:
            db.ctx.skipped["record"] += 1
            continue
        # the entry and its list slot; a new key its list, int and dict slot
        if not db.keep_flat(size + 8 + (0 if key in rows else 192)):
            db.ctx.skipped["record"] += 1
            break
        rows.setdefault(key, []).append(entry)
    return _Complex(kind, rows)


def _picture(db: _Db, definition: bytes):
    """An attachment's file bytes (`AttachmentColumnInfoImpl.decodeData`): a u32 type (0 raw, 1 deflated), the
    content's length n, the content (a header: its length, then the extension; the file). The bytes come from the
    document's one `flate.Budget(IMAGE_BYTES)` (a raw file too), the deflated ones through `flate.inflate`, its output
    no more than n (all Jackcess reads), than `flate`'s own cap, than the budget's room and than half of what the meter
    has left at `_INFLATE` bytes a unit. That most, twice (`flate` may run two passes, raw deflate then zlib), is
    charged before inflation: a failure keeps it all, a success gives back what its output fell short of that most
    (keeping one whole pass and the output). A
    definition is decoded once a document (`_Db.pictures`): a repeat takes its bytes of the budget again, or is counted
    `image` again when it failed. None, counted `image`, past the budget or when the wrapper cannot be read."""
    budget = db.images
    key = bytes(definition)
    if key in db.pictures:
        data = db.pictures[key]
        if data is not None and len(data) <= budget.total - budget.used:
            budget.used += len(data)
            return data
        db.ctx.skipped["image"] += 1
        return None
    room = budget.total - budget.used
    data = None
    try:
        if room <= 0:
            raise ReadError("too_large", "attachment pictures past IMAGE_BYTES")
        blob = db.long_bytes(definition, room + room // 1000 + 4096)  # a deflated file is a little longer
        if blob is None:
            raise ReadError("too_large", "attachment past IMAGE_BYTES")
        kind, n = struct.unpack_from("<iI", blob, 0)
        if kind == 1:
            stream = blob[8:]
            most = min(n, limits.CFB_STREAM, max(limits.ZIP_RATIO * len(stream), limits.ZIP_RATIO_FLOOR), room,
                       max(0, db.total - db.spent - 1) * _INFLATE // 2)
            reserved = 2 * most // _INFLATE + 1
            db.charge(reserved)
            content = flate.inflate(stream, budget, most)
            db.meter.give((most - len(content)) // _INFLATE)  # the work: one pass of `most` at most, then the output
        elif kind == 0:
            content = blob[8:]
            if len(content) > room:
                raise ReadError("too_large", "attachment past IMAGE_BYTES")
            budget.used += len(content)
        else:
            raise ValueError(f"attachment type {kind}")
        head = _u32(content, 0)
        if not 4 <= head <= n <= len(content):
            raise ValueError("attachment header past its data")
        data = bytes(content[head:n])
    except (ReadError, *_BAD):
        db.ctx.skipped["image"] += 1
    if db.keep(len(key)):
        db.pictures[key] = data
    return data


# -- rows

def _rows(db: _Db, tdef: Tdef, kept=()):
    """Each live row of the table, in page order, as (key, {column index: raw cell}) (see `_cells`): key is None, or
    for an overflow row the (tdef page, page, row) its pointer names. When `kept` holds that key the row is not read
    again and its cells are None (the caller keeps what it made of it). Only data pages naming this TDEF are read, so
    a map naming other tables' pages costs a page read each; a page of type 8 is skipped, counted `jet_page8`. Each
    page, slot and live row is charged first; a row that cannot be read is counted `record`; a bad usage map raises
    ValueError for the caller."""
    jet = db.jet
    for pg in jet.usage(tdef.owned):
        db.charge(_PAGE)
        page = jet.page(pg)
        if page[0] == _PAGE8:
            db.ctx.skipped["jet_page8"] += 1
            continue
        if page[0] != _DATA_PAGE or _u32(page, 4) != tdef.page:
            continue
        n = db.rows_on(page)
        db.charge(_SLOT * n)
        for r in range(n):
            if _u16(page, db.f.row_start + 2 * r) & _DELETED:
                continue
            db.charge(_ROW)
            try:
                buf, ptr = db.row_data(page, r)
                key = None if ptr is None else (tdef.page,) + ptr
                if key is not None and key in kept:
                    yield key, None
                    continue
                cells = _cells(db, tdef, buf if ptr is None else db.target(*ptr))
            except _BAD:
                db.ctx.skipped["record"] += 1
                continue
            yield key, cells


def _cells(db: _Db, tdef: Tdef, buf: bytes) -> dict:
    """One row's raw cells as {column index: a memoryview into the row, or True for Yes/No}: the column count, the
    fixed data at offset_F, the variable data through offset_V, the null mask at the end; offsets clipped to the row.
    Each mask byte looked at is charged `_GROUP`; the columns a set bit names (at most the row's count of them, column
    numbers being free to repeat) are charged `_CELL` each before they are visited; a null or "No" cell, or one past
    the count, is absent."""
    head = 1 if db.j3 else 2
    n = len(buf)
    if n < head:
        raise ValueError("row shorter than its column count")
    ncols = buf[0] if db.j3 else _u16(buf, 0)
    mlen = (ncols + 7) // 8
    end = n - mlen  # the null mask is the row's last bytes
    if end < head:
        raise ValueError("null mask past the row")
    mask = buf[end:]
    row = memoryview(buf)
    var = None
    out = {}
    groups = tdef.groups
    db.charge(_GROUP * min(len(groups), mlen))
    for byte, bits in groups:
        if byte >= mlen:
            break
        m = mask[byte]
        if not m:
            continue
        for bit in range(8):
            if not m >> bit & 1:
                continue
            named = bits[bit]  # one column number: all of them past the count, or the first ncols of them
            if not named or named[0][1].num >= ncols:
                continue
            db.charge(_CELL * min(len(named), ncols))
            for i, c in named:
                if i >= ncols:
                    break
                if c.type == BOOL and not c.ext & _CALCULATED:  # a calculated Yes/No is a wrapped value
                    out[i] = True
                elif c.flags & 0x01:
                    a = head + c.fixed_off
                    out[i] = row[min(a, end):min(a + FIXED_SIZE.get(c.type, c.length), end)]
                else:
                    if var is None:
                        if db.j3:
                            db.charge(buf[end - 1] + 1 + (n - 1) // 256)  # its offsets and jump entries
                            var = _jump_offsets(row, head, end)
                        else:
                            var = _var_offsets(row, head, end)
                    v = var(c.var)
                    if v is not None:
                        out[i] = v
    return out


def _var_offsets(buf: bytes, head: int, end: int):
    """Jet4 and ACE: the variable column count before the null mask, the two-byte offsets before it (reversed), the
    end of the data before them. Returns slot -> bytes (None past the row's count)."""
    if end - 2 < head:
        raise ValueError("no variable offset table")
    nvar = _u16(buf, end - 2)
    data_end = end - 4 - 2 * nvar  # the end-of-data offset
    if data_end < head:
        raise ValueError("variable offset table past the row")

    def get(i):
        if i >= nvar:
            return None
        at = end - 4 - 2 * i
        s, e = _u16(buf, at), _u16(buf, at - 2)
        s, e = max(s, head), min(e, data_end)
        return buf[s:e] if s < e else b""
    return get


def _jump_offsets(buf: bytes, head: int, end: int):
    """Jet3: one-byte offsets, each past 256 * the jump table entries reached (`readJumpTableVarColOffsets`)."""
    row_end = len(buf) - 1
    nvar = buf[end - 1]
    jumps = (len(buf) - 1) // 256
    col_offset = row_end - (len(buf) - end) - jumps - 1
    if col_offset - nvar < head:
        raise ValueError("variable offsets past the row")
    if (col_offset - nvar) // 256 < jumps:  # the last jump entry is a dummy
        jumps -= 1
    offsets, used = [], 0
    for i in range(nvar + 1):
        while used < jumps and i == buf[end - used - 2]:
            used += 1
        offsets.append(buf[col_offset - i] + used * 256)
    data_end = col_offset - nvar

    def get(i):
        if i >= nvar:
            return None
        s, e = max(offsets[i], head), min(offsets[i + 1], data_end)
        return buf[s:e] if s < e else b""
    return get


# -- values

def value(db: _Db, col: Column, raw):
    """A cell as shown: Yes/No, integers, Currency, Single and Double, Date, extended date, Decimal, GUID, Text (cut at
    JET_CELL_CHARS, counted `text`), Memo, OLE (its length, counted `object`) and binary (its length). Of the Format
    property only Percent and Yes/No are applied, to numbers; rich text (TextFormat 1) is the text of its HTML; a
    hyperlink Memo is a Link (a list of inlines) or "display (address)". A calculated column's value is its stored
    one (`_calc_value`). Raw bytes that do not fit the type raise ValueError (the caller counts `record`)."""
    t = col.type
    if col.ext & _CALCULATED:
        return "" if raw is None else _calc_value(db, col, raw)
    if t == BOOL:
        return "Yes" if raw else "No"
    if raw is None:
        return ""
    if t in _NUMBERS:
        s = _number(t, raw, col.scale)
        return _formatted(col.fmt, s) if col.fmt else s
    if t == COMPLEX:
        return str(struct.unpack("<i", raw)[0])
    if t == DATE:
        return _date(struct.unpack("<d", raw)[0])
    if t == EXTDATE:
        return _ext_date(bytes(_need(raw, 42)))
    if t == GUID:
        return "{" + str(uuid.UUID(bytes_le=bytes(_need(raw, 16)))).upper() + "}"
    if t == TEXT:
        return db.cut(db.decode(raw, bool(col.ext & 0x01)), rich=col.rich)
    if t == MEMO:
        s = db.memo(raw, bool(col.ext & 0x01), col.rich)
        return _hyperlink(db, s) if col.flags & _HYPERLINK else s
    if t == OLE:
        if len(raw) < 4:
            raise ValueError("OLE definition shorter than its length")
        db.ctx.skipped["object"] += 1
        return f"<OLE {_u32(raw, 0) & 0x3FFFFFFF} bytes>"
    return f"<binary {len(raw)} bytes>"


def _unwrap(data) -> bytes:
    """A calculated value's data (`CalculatedColumnUtil.unwrapCalculatedValue`): 16 bytes, a u32 length, the data (and
    three bytes); shorter than 20 bytes, as it is."""
    if len(data) < 20:
        return bytes(data)
    return bytes(data[20:20 + _u32(data, 16)])


def _calc_value(db: _Db, col: Column, raw) -> str:
    """A calculated column's stored value, of its result type: Yes/No is its first byte (none is "No"), text is in
    the compressed form whatever the ext byte says, a Memo a long value read as `memo` reads one (at most
    JET_LVAL_BYTES, a shorter read cut with "…"), Decimal its own form (`_calc_numeric`), the rest the type's bytes.
    Empty data is ""."""
    t, more = col.type, False
    if t == MEMO:
        if len(raw) < 12:
            raise ValueError("long value definition shorter than 12 bytes")
        head = _u32(raw, 0)
        length, kind = head & 0x3FFFFFFF, head >> 30
        want = min(length, JET_LVAL_BYTES)
        if kind == 3:
            raise ValueError("long value type 0xC0")
        data = raw[12:12 + want] if kind == 2 else db._lval((int.from_bytes(raw[5:8], "little"), raw[4]), want,
                                                             kind == 1)
        data, more = _unwrap(data), length > want
    else:
        data = _unwrap(raw)
    if t == BOOL:
        return "Yes" if data and data[0] else "No"
    if not data:
        return ""
    if t in (TEXT, MEMO):
        return db.cut(db.decode(data, True), more, col.rich)
    if t == NUMERIC:
        s = _calc_numeric(data)
        return _formatted(col.fmt, s) if col.fmt else s
    plain = Column(col.name, t, col.num, scale=col.scale, fmt=col.fmt)
    return value(db, plain, data[:FIXED_SIZE.get(t, len(data))])


def _calc_numeric(data: bytes) -> str:
    """A calculated Decimal (`CalcNumericColImpl.readCalcNumericValue`): an i16 length, a signed scale byte, a negate
    byte and at most 16 bytes, the first four swapped when their count is not a multiple of 8, then each eight
    reversed (`fixNumericByteOrder`); exact."""
    total = struct.unpack_from("<h", data, 0)[0]
    n = (total if total > 0 else len(data) - 2) - 2
    n = min(int(n / 4) * 4, 16)
    if n <= 0 or len(data) < 4 + n:
        raise ValueError(f"calculated Decimal of {n} bytes")
    scale = struct.unpack_from("<b", data, 2)[0]
    b = bytearray(data[4:4 + n])
    pos = 0
    if n % 8:
        b[0:4] = b[3::-1]
        pos = 4
    for k in range(pos, n, 8):
        b[k:k + 8] = b[k:k + 8][::-1]
    v = int.from_bytes(b, "big")
    d = Decimal((1 if data[3] and v else 0, tuple(int(ch) for ch in str(v)), -scale))
    return format(d, "f")


def _number(t: int, raw, scale: int) -> str:
    if t == BYTE:
        return str(_need(raw, 1)[0])
    if t in (INT, LONG, BIGINT):
        return str(struct.unpack({INT: "<h", LONG: "<i", BIGINT: "<q"}[t], raw)[0])
    if t == MONEY:
        return format(Decimal(f"{struct.unpack('<q', raw)[0]}e-4").normalize(), "f")
    if t == FLOAT:
        return _single(bytes(_need(raw, 4)))
    if t == DOUBLE:
        return repr(struct.unpack("<d", raw)[0])
    return _numeric(_need(raw, 17), scale)


def _formatted(fmt: str, s: str) -> str:
    """A number under its Format property: Percent is x 100 with "%" (exact, on the shown digits; a trailing ".0"
    dropped), Yes/No is "No" for 0 and "Yes" otherwise; any other format, or a value that is not finite, as it is."""
    if fmt not in ("percent", "yes/no"):
        return s
    try:
        d = Decimal(s)
    except InvalidOperation:
        return s
    if not d.is_finite():
        return s
    if fmt == "yes/no":
        return "No" if d == 0 else "Yes"
    with localcontext() as c:
        c.prec = 100
        d = d.scaleb(2)
        if not -30 <= d.adjusted() <= 30:  # far from 1: its exponent form, not tens of zeros
            return f"{d.normalize()}%"
    out = format(d, "f")
    if "." in out:
        out = out.rstrip("0").rstrip(".")
    return out + "%"


def _hyperlink(db: _Db, s: str):
    """A hyperlink value, `display#address#subaddress#tip` (Access's own form): a Link to address (and #subaddress)
    labelled display (or the address) when the address is http, https, mailto or ftp and holds no space or control
    character; else "display (address)". A value without "#" is its text. Each character is charged `_LINK` first."""
    db.charge(_LINK * len(s))
    display, sep, rest = s.partition("#")
    if not sep:
        return s
    address, _, rest = rest.partition("#")
    sub = rest.partition("#")[0]
    url = f"{address}#{sub}" if sub else address
    if not url:
        return display
    scheme, colon, tail = url.partition(":")
    if colon and tail and scheme.lower() in LINK_SCHEMES and not any(ch.isspace() or not ch.isprintable()
                                                                      for ch in url):
        return [Link(url, [Text(display or url)])]
    return f"{display} ({url})" if display else url


def _single(raw: bytes) -> str:
    """The shortest %g form that packs back to the same float32."""
    f = struct.unpack("<f", raw)[0]
    if not math.isfinite(f):
        return repr(f)
    for p in range(1, 10):
        s = "%.*g" % (p, f)
        try:
            if struct.pack("<f", float(s)) == raw:
                return s
        except OverflowError:
            continue
    return repr(f)


def _date(d: float) -> str:
    """Days since 1899-12-30; the fraction is a positive part of the day whatever the sign (Jackcess
    `fromLocalDateDouble`). The time is left out at midnight."""
    if not math.isfinite(d):
        raise ValueError("date is not a number")
    day = int(d)
    secs = round(abs(d) % 1.0 * 86400)
    if secs >= 86400:
        day, secs = day + 1, secs - 86400
    dt = datetime.datetime(1899, 12, 30) + datetime.timedelta(days=day, seconds=secs)
    return _iso(dt, 0)


def _ext_date(raw: bytes) -> str:
    """ACE's extended date: 19 digits of days since 0001-01-01, ':', 12 digits of seconds, 7 of 100 ns, ':7' and NUL
    (`readExtendedDateValue`)."""
    if raw[19:20] != b":" or not (raw[:19].isdigit() and raw[20:39].isdigit()):
        raise ValueError("extended date is not digits")
    dt = datetime.datetime(1, 1, 1) + datetime.timedelta(days=int(raw[:19]), seconds=int(raw[20:32]))
    return _iso(dt, int(raw[32:39]))


def _iso(dt: datetime.datetime, frac7: int) -> str:
    s = f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}"
    if dt.hour or dt.minute or dt.second or frac7:
        s += f" {dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
        if frac7:
            s += f".{frac7:07d}".rstrip("0")
    return s


def _numeric(raw: bytes, scale: int) -> str:
    """A sign byte, then 16 bytes as four little-endian u32s, the most significant first; exact, at the column's
    scale."""
    n = 0
    for k in range(4):
        n = n << 32 | _u32(raw, 1 + 4 * k)
    return format(Decimal(f"{-n if raw[0] else n}e-{scale}"), "f")


# -- catalog

def _catalog(db: _Db) -> list:
    """MSysObjects as `Obj` rows, at most JET_CATALOG (one more row counts `record` and ends the read)."""
    try:
        tdef = _tdef(db, 2)
    except _BAD as e:
        raise ReadError("damaged", f"jet: no readable catalog ({e})") from None
    except _Spent:
        db.ctx.skipped["record"] += 1
        return []
    at = {c.name.lower(): i for i, c in reversed(list(enumerate(tdef.columns)))}
    if not all(k in at for k in ("id", "name", "type", "flags")):
        raise ReadError("damaged", "jet: the catalog has no Id, Name, Type or Flags column")
    cols = tdef.columns

    def cell(cells, key, default=None):
        i = at.get(key)
        return default if i is None else (cells.get(i), cols[i])

    out, read = [], 0
    try:
        for _, cells in _rows(db, tdef):  # each catalog cell is charged; none is given back
            if read >= JET_CATALOG:
                db.ctx.skipped["record"] += 1
                break
            read += 1
            try:
                obj = _obj(db, cells, cell)
            except _BAD:
                db.ctx.skipped["record"] += 1
                continue
            if obj is not None:
                out.append(obj)
    except _Spent:
        db.ctx.skipped["record"] += 1  # the rest of the catalog, once
    except _BAD as e:
        raise ReadError("damaged", f"jet: no readable catalog ({e})") from None
    return out


def _obj(db, cells, cell):
    (oid, _), (name, ncol), (kind, _), (flags, _) = (cell(cells, k) for k in ("id", "name", "type", "flags"))
    if oid is None or name is None or kind is None or flags is None:
        return None
    kind = struct.unpack("<h", kind)[0]
    flags = struct.unpack("<I", flags)[0]
    parent = cell(cells, "parentid", (None, None))[0]
    obj = Obj(struct.unpack("<i", oid)[0], db.name(db.decode(name, bool(ncol.ext & 0x01))), kind, flags,
              struct.unpack("<i", parent)[0] if parent is not None else None)
    if kind == TABLE and not flags & SYSTEM_FLAGS:
        raw, pcol = cell(cells, "lvprop", (None, None))
        if raw is not None and pcol.type in (OLE, MEMO):
            inline = len(raw) >= 4 and _u32(raw, 0) >> 30 == 2
            definition = bytes(raw if inline else raw[:12])
            if db.keep(len(definition)):
                obj.lvprop = definition
            else:  # past the room: the table is shown without its properties
                db.ctx.skipped["record"] += 1
    if kind in (LINKED, LINKED_ODBC) and not flags & SYSTEM_FLAGS:
        connect, database, foreign = (_plain_text(db, *reversed(cell(cells, k, (None, None))))
                                      for k in ("connect", "database", "foreignname"))
        target, foreign = _linked_target(db, connect, database), db.name(foreign)
        if ";" in foreign or "=" in foreign:  # a table name holding a connect string's marks: not shown
            foreign = ""
        if db.keep(sys.getsizeof(target) + sys.getsizeof(foreign)):
            obj.connect, obj.foreign_name = target, foreign
        else:  # past the room: named without its target
            db.ctx.skipped["record"] += 1
    return obj


def _connect_parts(s: str):
    """A connect string's parts as (key, value, text), and whether it parsed cleanly. A part is `key=value` or a bare
    token (key None); parts end at ";". A value may be {braced} (ODBC: it ends at a "}" not followed by "}", "}}" being
    an escaped "}") or quoted with " or ' (a doubled quote escaped); either may hold ";" and "=", and only spaces may
    follow it before the next ";". A brace or quote left open, or text after one, is not clean: the parts read before
    it are returned with False. Keys are compared with their spaces collapsed, casefolded."""
    parts, i, n = [], 0, len(s)
    while i <= n:
        j = s.find(";", i)
        j = n if j < 0 else j
        eq = s.find("=", i, j)
        if eq < 0:
            parts.append((None, s[i:j].strip(), s[i:j].strip()))
            i = j + 1
            continue
        key = " ".join(s[i:eq].split()).casefold()
        v = eq + 1
        while v < j and s[v] in " \t":
            v += 1
        close = {"{": "}", '"': '"', "'": "'"}.get(s[v:v + 1]) if v < n else None
        if close is None:
            value = s[v:j].strip()
        else:
            pos = v + 1
            while True:
                k = s.find(close, pos)
                if k < 0:
                    return parts, False
                if s[k + 1:k + 2] == close:  # an escaped "}}", or a doubled quote
                    pos = k + 2
                    continue
                break
            value = s[v:k + 1]
            j = s.find(";", k + 1)
            j = n if j < 0 else j
            if s[k + 1:j].strip():
                return parts, False
        parts.append((key, value, f"{s[i:eq].strip()}={value}"))
        i = j + 1
    return parts, True


def _safe_value(value: str) -> str:
    """A value fit to show, showing less rather than a password: its authority's user part taken out, up to and with
    the authority's last "@" (`_AUTHORITY`: as browsers read it, so `u:p@w@h`, spaces, `http:\\` and `https:/`
    too); "" when it holds ";", "=" or a control character (what a value of a secret may spill into), when its
    authority, before its user part is taken out, holds a ":" or an "@" and another "@" follows it (`u:pa/ss@h`,
    `u:p@s3/cret@h`: the authority would end inside the password), or when
    a segment of it still has a ":" before an "@" (user info this reading did not place)."""
    value = value.strip()
    m = _AUTHORITY.match(value)
    authority, rest = m.group(2), value[m.end():]
    # a user part (judged before it is taken out) holding "/", "?", "#" or "\": its tail would be the path
    if ("@" in authority or ":" in authority) and "@" in rest:
        return ""
    if "@" in authority:
        value = (m.group(1) or "") + authority.rpartition("@")[2] + rest
    if ";" in value or "=" in value or _CONTROL.search(value):
        return ""
    if "@" in value and any(":" in s[:s.rfind("@")] for s in _SEGMENTS.split(value) if "@" in s):
        return ""
    return value


def _linked_target(db: _Db, connect: str, database: str) -> str:
    """What a linked table's connect string names, on one line, showing less rather than a credential: its DATABASE=
    part, else the catalog's Database cell (an Access link keeps its path there; only when it holds no ";" or "="),
    else the string's known safe parts (`_SAFE_KEYS`, and a bare first token such as "ODBC") joined with ";". Any other
    key (PWD=, UID=, Jet OLEDB:Database Password=, Token= ...) is never shown, nor a value holding ";", "=" or a
    control character, nor a URL's user part. A string that does not parse cleanly (`_connect_parts`) shows only the
    file name of a DATABASE= read before the fault, or nothing. Its characters and marks are charged before it is
    parsed."""
    marks = connect.count(";") + connect.count("}") + connect.count('"') + connect.count("'") + 1
    db.charge(len(connect) + _PART * marks)
    parts, ok = _connect_parts(connect)
    found = next((_safe_value(v) for k, v, _ in parts if k == "database"), "")
    if not ok:
        return _safe_value(re.split(r"[\\/]", found)[-1])
    if found:
        return found
    cell = _safe_value(database)
    if cell:
        return cell
    return ";".join(text for i, (k, v, text) in enumerate(parts) if _safe_part(i, k, v, False))


def _safe_part(i: int, k, v: str, query: bool) -> bool:
    """Whether a connect string's part (`_connect_parts`) may show: a bare first token or a `_SAFE_KEYS` part with a
    value, its value one `_safe_value` leaves as it is; for a query (`_clean_connect`) also an empty bare part, and
    an empty first one."""
    value = v.strip()
    if _safe_value(v) != value:
        return False
    if k is None:
        return i == 0 and (query or bool(value)) or query and not value
    return k in _SAFE_KEYS and bool(value)


# -- saved queries

def _clean_connect(db: _Db, s: str):
    """A connect position of a query (`jet_sql`'s `connect`): the string cleaned as a linked table's, showing less
    rather than a credential, and whether anything was withheld. Its characters and marks are charged first. The
    parts `_safe_part` keeps stay as they are; each run of parts it drops becomes one "…" part (an "…" already there
    is kept, so cleaning again changes nothing); a string that does not parse cleanly (`_connect_parts`) is "…"."""
    marks = s.count(";") + s.count("}") + s.count('"') + s.count("'") + 1
    db.charge(len(s) + _PART * marks)
    parts, ok = _connect_parts(s)
    if not ok:
        return "…", True
    out, dropped = [], False
    for i, (k, v, text) in enumerate(parts):
        if k is None and v == "…" or _safe_part(i, k, v, True):
            out.append(text)
            continue
        dropped = True
        if not out or out[-1] != "…":
            out.append("…")
    return ";".join(out), dropped


def _clean_urls(db: _Db, s: str):
    """`s` with each URL's user part taken out, as `_safe_value` reads one, put as "…" ("http://…@host/x"), and
    whether any was. A URL here is a run of two or more slashes or backslashes, or one after a ":"; its authority runs
    to the next "/", "\\", "?" or "#" (or the end of its word: a space, quote, bracket, parenthesis, "<", ">" or
    ","). The user part is up to the authority's last "@"; an authority holding ":" and no "@" whose word holds one
    later (`u:pa/ss@h`) gives up to that "@". Its characters are charged first; one pass."""
    db.charge(len(s))
    out, last, dropped = [], 0, False
    word_end, word_at = -1, -1
    for m in _SLASHES.finditer(s):
        a = m.end()
        if a <= last or m.end() - m.start() < 2 and not (m.start() and s[m.start() - 1] == ":"):
            continue
        if a >= word_end:
            stop = _WORD_STOP.search(s, a)
            word_end = len(s) if stop is None else stop.start()
            word_at = s.rfind("@", a, word_end)
        stop = _AUTH_STOP.search(s, a, word_end)
        b = word_end if stop is None else stop.start()
        at = s.rfind("@", a, b)
        if at < 0 and word_at >= a and ":" in s[a:b]:
            at = word_at
        if at < 0:
            continue
        if s[a:at] != "…":
            dropped = True
        out.append(s[last:a])
        out.append("…")
        last = at
    out.append(s[last:])
    return "".join(out), dropped


def _query_int(col: Column, raw):
    """A Byte, Integer or Long cell of MSysQueries; None when null."""
    if raw is None:
        return None
    fmt = {BYTE: "<B", INT: "<h", LONG: "<i"}.get(col.type)
    if fmt is None:
        raise ValueError(f"type {col.type} where a number was expected")
    return struct.unpack(fmt, raw)[0]


def _query_text(db: _Db, col: Column, raw):
    """A Text or Memo cell of MSysQueries, whole (a Memo of at most eight times JET_SQL bytes, else ValueError), its
    bytes charged as decoded; None when null."""
    if raw is None:
        return None
    if col.type == TEXT:
        return db.decode(raw, bool(col.ext & 0x01))
    if col.type != MEMO:
        raise ValueError(f"type {col.type} where text was expected")
    data = db.long_bytes(raw, 8 * jet_sql.JET_SQL + 2)
    if data is None:
        raise ValueError("query text past its cap")
    return db.decode(data, bool(col.ext & 0x01))


def _query_rows(db: _Db, catalog: list, queries: list):
    """{query id: its MSysQueries rows as `jet_sql.Row`s in table order, or None when they are not kept}, or None
    when MSysQueries is missing or cannot be read (a row that cannot be read names no query, so none is trusted). Rows
    are charged as any table's and `_QROW` each before they are made; a row of another object is skipped before its
    text is read. A query's rows are not kept (None) past `jet_sql.JET_QUERY_ROWS`, when a cell of one cannot be read
    or a Memo is past its cap, or past the queries' room, four times the file's bytes (a row 128 bytes and its
    strings)."""
    t = next((o for o in catalog if o.type == TABLE and o.flags & SYSTEM_FLAGS and o.name.lower() == "msysqueries"),
             None)
    if t is None or db.meter.over:
        return None
    ids = {o.id for o in queries}
    out, room = {}, 4 * db.jet.pages * db.jet.page_size
    before = db.ctx.skipped["record"]
    try:
        tdef = _tdef(db, t.id & 0xFFFFFF)
        at = {c.name.lower(): i for i, c in reversed(list(enumerate(tdef.columns)))}
        if "objectid" not in at or "attribute" not in at:
            raise ValueError("MSysQueries has no ObjectId or Attribute column")
        cols = tdef.columns

        def cell(cells, key):
            i = at.get(key)
            return (None, None) if i is None else (cols[i], cells.get(i))

        for _, cells in _rows(db, tdef):
            oid = _long(*cell(cells, "objectid"))
            rows = out.get(oid, [])
            if oid not in ids or rows is None:
                continue
            db.charge(_QROW)
            try:
                texts = [_query_text(db, *cell(cells, k)) for k in ("expression", "name1", "name2")]
                ints = [_query_int(*cell(cells, k)) for k in ("attribute", "flag", "lvextra")]
                col, raw = cell(cells, "order")
                if raw is not None and col.type != BINARY:
                    raise ValueError(f"type {col.type} where Order's bytes were expected")
                order = None if raw is None else bytes(raw)
            except _BAD:
                out[oid] = None
                continue
            size = 128 + sum(sys.getsizeof(v) for v in (*texts, order) if v is not None)
            if len(rows) >= jet_sql.JET_QUERY_ROWS or size > room:
                out[oid] = None
                continue
            room -= size
            rows.append(jet_sql.Row(ints[0], texts[0], ints[1], texts[1], texts[2], oid, order, ints[2]))
            out[oid] = rows
    except (_Spent, *_BAD):
        return None
    return None if db.ctx.skipped["record"] > before else out


def _query(db: _Db, obj: Obj, rows) -> Section:
    """A query's section, "Query: <name>", holding its SQL (`jet_sql.sql`, its work charged first through
    `Meter.afford`) as a Code block; with no SQL (rows not kept, Jackcess unable to write it, the meter) the heading
    stays alone, counted `query`. Its connect positions go through `_clean_connect` and its text through
    `_clean_urls`; a query whose SQL shows with anything withheld ("…") counts `withheld` once. A control character
    other than a tab or line break shows as a space."""
    text = None
    withheld = []

    def clean(fn, s):
        out, dropped = fn(db, s)
        if dropped:
            withheld.append(True)
        return out
    try:
        db.charge(_QUERY)
        if rows is not None and db.meter.afford(_SQL * jet_sql.work(rows)):
            text = jet_sql.sql(rows, flags=obj.flags, connect=lambda s: clean(_clean_connect, s),
                               urls=lambda s: clean(_clean_urls, s))
    except _Spent:
        text = None
    blocks = []
    if text and text.strip():
        blocks.append(Code(_SQL_CONTROL.sub(" ", text), "sql"))
        if withheld:
            db.ctx.skipped["withheld"] += 1
    else:
        db.ctx.skipped["query"] += 1
    if obj.flags & HIDDEN_FLAG:
        return Section("query", obj.name, blocks=_hide(blocks), hidden="hidden")
    return Section("query", obj.name, blocks=blocks)


# -- output

def _hide(blocks: list) -> list:
    """Hidden copies of a hidden table's blocks, so showing hidden content marks them (as xls's hidden sheets)."""
    out = []
    for b in blocks:
        c = copy.copy(b)
        c.hidden = "hidden"
        out.append(c)
    return out


def _count(n: int, one: str, many: str) -> str:
    return f"{n:,} {one if n == 1 else many}"


def _shown(db: _Db, cols: list, cells: dict, bools: list, cplx=None) -> list:
    """A row's non-empty values as (column index, value); a Yes/No cell absent from the row is "No"; a complex cell
    is its column's `_Complex.cell` ("" when the column could not be resolved). A visited cell that gives a value has
    its `_CELL` given back: the grid charges it from there."""
    out = []
    for i, raw in cells.items():
        try:
            if cplx and i in cplx:
                info = cplx[i]
                v = info.cell(db, raw) if info else ""
            else:
                v = value(db, cols[i], raw)
        except _BAD:
            db.ctx.skipped["record"] += 1
            continue
        if v:
            out.append((i, v))
    db.meter.give(_CELL * len(out))
    for i in bools:
        if i not in cells:
            out.append((i, "No"))
    return out


def _schema(db: _Db, tdef: Tdef, name: str, rels: dict, cplx=None) -> list:
    """The "Columns:" line as inlines: each column's name, type ("Attachment" for an attachment column), the targets
    its relationships name (`_REF` each, charged first), a calculated column's "= expression" (on one line, cut at
    JET_CELL_CHARS, counted `text`; its characters charged first), PK, autonumber, required and rich text. A hidden
    column is a Hidden inline holding its separator (the one before it, or after it when no shown column comes
    first), so the shown line reads right either way."""
    pk = set(tdef.pk)
    table = name.lower()
    cols = tdef.columns
    out, text, shown = [], ["Columns: "], False
    for k, c in enumerate(cols):
        info = cplx.get(k) if cplx else None
        kind = "Attachment" if info and info.kind == "attachment" else TYPE_NAMES.get(c.type, f"type {c.type}")
        targets = rels.get((table, c.name.lower()))
        if targets:
            db.charge(_REF * len(targets))
            kind += "".join([f" → {t}" for t in targets])
        bits = [kind]
        if c.ext & _CALCULATED and c.expr:
            db.charge(len(c.expr))
            bits.append("= " + db.cut(_CONTROL.sub(" ", c.expr).strip()))
        if c.num in pk:
            bits.append("PK")
        if c.flags & _AUTONUMBER:
            bits.append("autonumber")
        if c.required:
            bits.append("required")
        if c.rich:
            bits.append("rich text")
        piece = f"{c.name} ({', '.join(bits)})"
        if not c.hidden:
            text.append(" · " + piece if shown else piece)
            shown = True
            continue
        if text:
            out.append(Text("".join(text)))
            text = []
        if shown:
            piece = " · " + piece
        elif k + 1 < len(cols):
            piece += " · "
        out.append(Hidden([Text(piece)]))
    if text:
        out.append(Text("".join(text)))
    return out


def _key_columns(tdef: Tdef) -> list:
    """The column indexes of the primary key (at most ten, an index's limit), the first column of each number."""
    first = {}
    for i, c in enumerate(tdef.columns):
        first.setdefault(c.num, i)
    return [first[n] for n in tdef.pk if n in first]


def _sort_key(db: _Db, cols: list, keys: list, cells: dict, shown: list) -> tuple:
    """A row's primary-key values as typed tuples, `_KEY` a column charged first: null first, numbers and dates by
    value (NaN last), text without case (from the value shown, a unit a character charged first: the sort compares
    it), other bytes as they are; a value that cannot be read after all of them."""
    db.charge(_KEY * len(keys))
    texts = None
    out = []
    for i in keys:
        c, raw = cols[i], cells.get(i)
        t = c.type
        if t == BOOL:
            out.append((1, raw is not None))
            continue
        if raw is None:
            out.append((0,))
            continue
        try:
            if t == MONEY:
                v = struct.unpack("<q", raw)[0]
            elif t in (BYTE, INT, LONG, BIGINT, COMPLEX):
                v = int(_number(LONG if t == COMPLEX else t, raw, 0))
            elif t in (FLOAT, DOUBLE, DATE):
                v = struct.unpack("<f" if t == FLOAT else "<d", raw)[0]
                if v != v:
                    out.append((2,))
                    continue
            elif t == NUMERIC:  # one scale a column: its signed integer orders it
                _need(raw, 17)
                v = int.from_bytes(b"".join(bytes(raw[k:k + 4])[::-1] for k in (1, 5, 9, 13)), "big")
                v = -v if raw[0] else v
            elif t in (TEXT, MEMO):
                if texts is None:
                    texts = dict(shown)
                v = texts.get(i, "")
                v = v if isinstance(v, str) else ""
                db.charge(len(v))  # its casefold, and its share of each comparison it takes part in
                v = v.casefold()
            else:
                v = bytes(raw)
        except _BAD:
            out.append((3,))
            continue
        out.append((1, v))
    return tuple(out)


class _Order:
    """A keyed table's rows held for the primary-key sort, while they fit what the grid has left: at most
    SHEET_ROWS - 1 rows (the header is row 0), and the cells and characters the shared budget still holds. So what is
    held is bounded by the grid's own caps."""

    def __init__(self, budget):
        self.budget, self.rows, self.cells, self.chars = budget, [], 0, 0

    def add(self, key, shown) -> bool:
        """Hold a row; False, holding nothing, when it does not fit."""
        cells = self.cells + len(shown)
        chars = self.chars + sum(sheet_grid._chars(v) for _, v in shown)
        b = self.budget
        if len(self.rows) + 1 > limits.SHEET_ROWS - 1 or cells > b.cells or chars > b.chars:
            return False
        self.rows.append((key, shown))
        self.cells, self.chars = cells, chars
        return True


def _table(db: _Db, obj: Obj, budget, rels: dict, objs=()):
    """The table's Section; None when the meter runs out on its TDEF, its properties, its complex columns or its schema
    line (counted `db_table`, as a table not read). A table with a primary key shows its rows in key order when they
    all fit the grid and the meter affords the sort; else in page order. The pictures of the rows shown follow the
    grid, in their order, alt "<table> <key>: <file name>" (the key the row's primary key values, or its row number)."""
    try:
        tdef = _tdef(db, obj.id & 0xFFFFFF)
        _apply_props(db, tdef, db.props_of(obj))
        cplx = {i: _complex_info(db, objs, tdef, c) for i, c in enumerate(tdef.columns) if c.type == COMPLEX}
        line = _schema(db, tdef, obj.name, rels, cplx)
    except _Spent:
        db.ctx.skipped["db_table"] += 1
        return None
    title = f"{obj.name} ({_count(tdef.row_count, 'row', 'rows')})"
    grid = sheet_grid.Grid(obj.name, budget, db.ctx)
    cols = tdef.columns
    for i, c in enumerate(cols):
        if not grid.put(0, i, c.name):
            break
    for i, c in enumerate(cols):
        if c.hidden:
            grid.hide_cols(i, i)
    # "No" past a row's count: each one a grid cell (a calculated Yes/No is a stored value)
    bools = [i for i, c in enumerate(cols) if c.type == BOOL and not c.ext & _CALCULATED]
    keys = _key_columns(tdef)
    order = _Order(budget) if keys else None
    # an overflow target's key -> (its non-empty values, the counts making them added, its sort key); room allowing
    done = {}
    r = 1
    pics, pics_cut = [], False

    def put(shown):
        nonlocal r, pics_cut
        files = []
        for i, v in shown:
            if isinstance(v, _Files):
                if not grid.put(r, i, str(v)):
                    break
                files.append((i, v))
            elif not grid.put(r, i, v):
                break
        if files and not pics_cut:
            got = dict(shown)
            key = ", ".join(v for v in (got.get(k) for k in keys) if isinstance(v, str) and v) or str(r)
            try:
                for i, v in files:
                    v.info.pictures(db, v.fk, f"{obj.name} {key}", cols[i].hidden, pics)
            except _Spent:  # the rest of the table's pictures, counted once
                pics_cut = True
                db.ctx.skipped["image"] += 1
        r += 1

    def flush():
        nonlocal order
        held, order = order.rows, None
        for _, shown in held:
            if grid.cut:
                break
            put(shown)

    try:
        for key, cells in _rows(db, tdef, done):
            if grid.cut:
                break
            if cells is None:  # a row many pointers name: read once, its values and counts repeated for each
                shown, made, sk = done[key]
                db.ctx.skipped.update(made)
            else:
                before = collections.Counter(db.ctx.skipped) if key is not None else None
                shown = _shown(db, cols, cells, bools, cplx)
                sk = _sort_key(db, cols, keys, cells, shown) if order is not None else None
                if key is not None and shown and db.keep(sum(64 + sys.getsizeof(v) for _, v in shown)):
                    done[key] = shown, db.ctx.skipped - before, sk  # nothing shown: read again
            if order is not None:
                if order.add(sk, shown):
                    continue
                flush()  # past the grid: page order from here
            put(shown)
    except _Spent:
        if order is not None:
            flush()
        if not grid.cut:
            grid.stop("units of work", db.total)
    if order is not None:
        n = len(order.rows)
        if db.meter.afford(_SORT * n * n.bit_length()):
            order.rows.sort(key=itemgetter(0))  # stable: equal keys keep their page order
        flush()
    blocks = [Para(line)] + grid.blocks() + pics
    if obj.flags & HIDDEN_FLAG:
        return Section("table", title, blocks=_hide(blocks), hidden="hidden")
    return Section("table", title, blocks=blocks)


def parse_bytes(data, ctx) -> Doc:
    """An Access database held in memory: the summary line, a table section per user table, then a query section
    per saved query."""
    jet = jet_pages.open(data)
    _refuse_encrypted(jet)
    db = _Db(jet, ctx)
    catalog = _catalog(db)
    rels = _relationships(db, catalog)
    objs = [o for o in catalog if not o.flags & SYSTEM_FLAGS]
    tables = [o for o in objs if o.type == TABLE]
    queries = [o for o in objs if o.type == QUERY]
    linked = [o for o in objs if o.type in (LINKED, LINKED_ODBC)]
    others = [o for o in objs if o.type in (FORM, REPORT, MACRO, MODULE)]
    parts = [_count(len({o.id & 0xFFFFFF for o in tables}), "table", "tables")]
    if queries:
        parts.append(_count(len(queries), "query", "queries"))
    if linked:
        parts.append(_count(len(linked), "linked table", "linked tables"))
    head = _summary(db, catalog, f"{VERSION_NAMES[jet.version]}: {', '.join(parts)}.")
    budget = sheet_grid.Budget(limits.SHEET_CELLS, limits.SHEET_CHARS)
    out, seen = [], set()
    for o in tables:
        page = o.id & 0xFFFFFF
        if page in seen:  # a second catalog row for one TDEF
            ctx.skipped["record"] += 1
            continue
        seen.add(page)
        if db.meter.over:  # the work total is spent: no further table is read
            ctx.skipped["db_table"] += 1
            continue
        sec = None
        with ctx.guard("db_table"):
            sec = _table(db, o, budget, rels, catalog)
        if sec is not None:
            out.append(sec)
    qrows = _query_rows(db, catalog, queries) if queries else None
    for o in queries:
        sec = None
        with ctx.guard("query"):
            sec = _query(db, o, None if qrows is None else qrows.get(o.id, []))
        if sec is not None:
            out.append(sec)
    if tables and not out and not (queries or linked or others) and not db.meter.over:
        raise ReadError("damaged", "no table could be read")
    return Doc(blocks=[Para(head)] + out)


def _summary(db: _Db, catalog: list, line: str) -> list:
    """The summary paragraph's inlines: `line`; then, each on its own line, "Forms: … Reports: … Macros: … Modules: …
    (names only)." (each part only when it has names; a module counts `vba`) and "Linked tables: <name> → <target>
    (<table>)" joined with "; " (counted `linked_data`). System objects and the temporary objects Access names with
    a leading "~" (and hides in its navigation pane) are left out. Each name is charged `_NAME` before it is written;
    the meter running out (the catalog cut by it) ends the names there, counted `record` once."""
    out = [Text(line)]
    groups = {FORM: [], REPORT: [], MACRO: [], MODULE: []}
    linked = []
    try:
        for o in catalog:
            if o.flags & SYSTEM_FLAGS or o.name.startswith("~"):
                continue
            if o.type in groups:
                db.charge(_NAME)
                groups[o.type].append(o.name)
            elif o.type in (LINKED, LINKED_ODBC):
                db.charge(_NAME)
                s = f"{o.name} → {o.connect}" if o.connect else o.name
                linked.append(f"{s} ({o.foreign_name})" if o.foreign_name else s)
    except _Spent:
        db.ctx.skipped["record"] += 1
    parts = [f"{label}: {', '.join(groups[kind])}" for label, kind in (("Forms", FORM), ("Reports", REPORT),
                                                                       ("Macros", MACRO), ("Modules", MODULE))
             if groups[kind]]
    if parts:
        out += [Break(), Text(". ".join(parts) + " (names only).")]
    if groups[MODULE]:
        db.ctx.skipped["vba"] += 1
    if linked:
        out += [Break(), Text("Linked tables: " + "; ".join(linked))]
        db.ctx.skipped["linked_data"] += 1
    return out


def parse(path, ctx) -> Doc:
    return parse_bytes(Path(path).read_bytes(), ctx)
