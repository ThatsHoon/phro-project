# Ported from MPXJ (https://www.mpxj.org), copyright (c) Packwood Software, under the GNU Lesser General Public
# License 2.1 or later. This module is distributed under the same license: see https://www.gnu.org/licenses/lgpl-2.1.
"""Microsoft Project's blocks (MPP9, MPP12, MPP14): Props, VarMeta, Var2Data, FixedMeta and FixedData, the field maps
that place each field in them, and the encodings of their values. A port of MPXJ's org.mpxj.mpp Props9/12/14,
VarMeta9/12, Var2Data, FixedMeta, FixedData, FieldMap, FieldMap9/12/14, MPPUtility and VarDataFieldReader, and of the
field tables of org.mpxj.common.MPPTaskField, MPPResourceField and MPPAssignmentField.

Where MPXJ reads past the end of a block (an exception that fails the whole file), a value that does not fit is
missing here instead, and a list that runs short ends where its bytes do. A bad magic number in a VarMeta or FixedMeta
is BlockError, the file's `damaged`.

Field maps. A Props field map is 28-byte entries: a mask, the u16 offset in the fixed data (65535: none), the var data
key (a byte at 6 in MPP9, the field id's low 16 bits in MPP12 and MPP14), the u32 field id and the u16 category (0x0B
and 0x64: the meta data). The fixed data offsets of one block rise; one that falls starts the second block. Each
block's largest offset plus its field's size is its `max` (MPXJ's getMaxFixedDataSize), from the size of every field
the map names (_SIZES, by the field's data type, generated from MPXJ's tables). A file without a field map uses the
version's default map, whose `max` stays 0. Only the fields the reader shows are named (_FIELDS); the others count
toward `max` alone. A field named twice keeps its last entry. MPP14 names fields 29, 35 and 36 its scheduled duration,
start and finish, and fields 27, 43 and 44 the estimated baseline's duration, start and finish.
"""
import math
import struct
from datetime import datetime, timedelta

EPOCH = datetime(1983, 12, 31)
MAGIC = 0xFADFADBA
TASK_BASE, RESOURCE_BASE, ASSIGNMENT_BASE = 0x0B400000, 0x0C400000, 0x0F400000
VALUE_LISTS = (0x0701, 0x0401)

# MPXJ TimeUnit names, by the low 5 bits of an MPP duration's units (MPPUtility.getDurationTimeUnits)
_UNITS = {3: "MINUTES", 4: "ELAPSED_MINUTES", 5: "HOURS", 6: "ELAPSED_HOURS", 7: "DAYS", 8: "ELAPSED_DAYS",
          9: "WEEKS", 10: "ELAPSED_WEEKS", 11: "MONTHS", 12: "ELAPSED_MONTHS", 19: "PERCENT", 20: "ELAPSED_PERCENT"}
# tenths of a minute in one unit (MPPUtility.getDuration)
_TENTHS = {"MINUTES": 10, "ELAPSED_MINUTES": 10, "HOURS": 600, "ELAPSED_HOURS": 600, "DAYS": 4800,
           "ELAPSED_DAYS": 14400, "WEEKS": 24000, "ELAPSED_WEEKS": 100800, "MONTHS": 96000, "ELAPSED_MONTHS": 432000}
CONSTRAINTS = ("AS_SOON_AS_POSSIBLE", "AS_LATE_AS_POSSIBLE", "MUST_START_ON", "MUST_FINISH_ON",
               "START_NO_EARLIER_THAN", "START_NO_LATER_THAN", "FINISH_NO_EARLIER_THAN", "FINISH_NO_LATER_THAN",
               "START_ON", "FINISH_ON")

# (field, data type, units field) by MPP field index
_TASK = {14: ("name", "STRING"), 15: ("notes", "NOTES"), 16: ("wbs", "STRING"), 17: ("constraint", "CONSTRAINT"),
         23: ("id", "INTEGER"), 24: ("milestone", "BOOLEAN"),
         27: ("baseline_duration", "DURATION", "baseline_duration_units"), 43: ("baseline_start", "DATE"),
         44: ("baseline_finish", "DATE"), 179: ("baseline_duration_units", "TIME_UNITS"), 26: ("subproject_file", "STRING"), 29: ("duration", "DURATION", "duration_units"),
         32: ("percent", "PERCENTAGE"), 35: ("start", "DATE"), 36: ("finish", "DATE"), 39: ("late_start", "DATE"),
         40: ("late_finish", "DATE"), 41: ("actual_start", "DATE"),
         42: ("actual_finish", "DATE"), 85: ("outline_level", "SHORT"),
         86: ("unique_id", "INTEGER"), 93: ("created", "DATE"), 94: ("notes", "NOTES"),
         181: ("duration_units", "TIME_UNITS"), 232: ("external_task", "BOOLEAN"),
         242: ("subproject_task_unique_id", "INTEGER"), 249: ("outline_level", "SHORT"),
         255: ("subproject_task_id", "INTEGER"), 1279: ("active", "BOOLEAN"), 1283: ("start", "DATE"),
         1284: ("finish", "DATE"), 1288: ("manual_duration", "DURATION", "manual_units"),
         1289: ("manual_units", "TIME_UNITS"), 1338: ("scheduled_start", "DATE"), 1339: ("scheduled_finish", "DATE"),
         1340: ("scheduled_duration", "DURATION", "duration_units"), 1299: ("baseline_start", "DATE"),
         1300: ("baseline_finish", "DATE"), 1301: ("baseline_duration", "DURATION", "baseline_duration_units")}
_TASK14 = {**_TASK, 27: ("baseline_estimated_duration", "DURATION", "baseline_duration_units"),
           29: ("scheduled_duration", "DURATION", "duration_units"), 35: ("scheduled_start", "DATE"),
           36: ("scheduled_finish", "DATE"), 43: ("baseline_estimated_start", "DATE"),
           44: ("baseline_estimated_finish", "DATE")}
_RESOURCE = {0: ("id", "INTEGER"), 1: ("name", "STRING"), 4: ("max_units", "UNITS"), 27: ("unique_id", "INTEGER"),
             57: ("available_from", "DATE"), 58: ("available_to", "DATE"), 276: ("availability", "BINARY"),
             808: ("active", "BOOLEAN")}
_ASSIGNMENT = {0: ("unique_id", "INTEGER"), 1: ("task_unique_id", "INTEGER"), 2: ("resource_unique_id", "INTEGER"),
               7: ("units", "UNITS")}

# the fixed data size of each field index (0 1 2 4 8, g = 16), generated from MPXJ's field tables and data types
_TASK_SIZES = (
    "8884888888888800024244442204444422244444444444400000440440440440440000022222222222280240000024000024"
    "4404448882220000000480000000220020000000000000000000200000004004884088800000002202022200000000000000"
    "2020000000000000000000002200000020000000004000200222220400888888844444444444444444444444444422222222"
    "2200000000000000000000000000000000000222222200000000000000004000000004404000000000000004000000002002"
    "0402222222222200040404040404040404040444080000000000000000400000000000000000000080448804280004488042"
    "8000448804280004488042800044880428000002280044880428000448804280004488042800044880428000448804280008"
    "8888888884444444444444444444444444444444444444444222222222222222222222222222222000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000002222222222888888888844444444444444444444444444444444444444440"
    "0000000000000000000000000000000000000000000000000000000000000000000002222222222222222222200000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000200200000000000000008800gg0g0000044000000000002000g088244880024488002448800244880"
    "0244880024488002448800244880024488002448800244880020000000000000000000000000000202244000420000022204"
    "4444444444444444444444444444444420080044400000000000000000000000000000000000000002222000000000000000"
    "00000000004444"
)
_RESOURCE_SIZES = (
    "4000808800088888888208848284020000200088800000088808888004400000000000220000000000000000000000000000"
    "0044444444440000044422288822222222220000000020000000400000000002008888888444444444444444444444422222"
    "2222200000000000000044444000000000000000000002222222040002844244242282442000000404040404040404040400"
    "0800002000000000000000000000000000000000008800004400880000440088000044008800004400880000440088000044"
    "0088000044008800004400880000440088000044000400888888888844444444444444444444444444444444444444442222"
    "2222222222222222222222222200000000000000000000000000000000000000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000022200220422000"
    "0000000000000000000088000040gg0000000000000000000000288288008800880088008800880088008800880088008800"
    "200000002000000000000000000000000000000000000000000004004008"
)
_ASSIGNMENT_SIZES = (
    "4444400888222222222222222222228888888888444444444444444444440000000000000000000020000000004444444444"
    "0000000000000000000000000000002220020000000004440000000000088888884444444444444444444444222222222200"
    "0000000000000444440000000000000000000022222220488000000000000044400000180000000000000000088000044088"
    "0000440880000440880000440880000440880000440880000440880000440880000440880000440008888888888444444444"
    "4444444444444444444444444444444000000000022222222222222222222000000000000000000000000000000000000000"
    "0000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000002000000000000000000000000880040ggg0000000000000000000000000000008800880088008800880088008800880"
    "088008800880088"
)
_SIZES = {TASK_BASE: _TASK_SIZES, RESOURCE_BASE: _RESOURCE_SIZES, ASSIGNMENT_BASE: _ASSIGNMENT_SIZES}
_SIZE_CODES = {"0": 0, "1": 1, "2": 2, "4": 4, "8": 8, "g": 16}

FIXED, VAR, META, UNKNOWN = "fixed", "var", "meta", "unknown"
F, V = FIXED, VAR
# the default maps (FieldMap9/12/14 getDefault*Data), the fields read here: (field, location, block, offset, var key)
DEFAULTS = {
    (9, "task"): (("unique_id", F, 0, 0, 0), ("id", F, 0, 4, 0), ("late_start", F, 0, 12, 0),
                  ("outline_level", F, 0, 40, 0), ("duration", F, 0, 60, 0), ("duration_units", F, 0, 64, 0),
                  ("constraint", F, 0, 80, 0), ("start", F, 0, 88, 0), ("finish", F, 0, 92, 0),
                  ("actual_start", F, 0, 96, 0), ("percent", F, 0, 122, 0), ("created", F, 0, 130, 0),
                  ("actual_finish", F, 0, 100, 0), ("late_finish", F, 0, 152, 0), ("wbs", V, 0, 65535, 10),
                  ("name", V, 0, 65535, 11), ("subproject_task_unique_id", V, 0, 65535, 9),
                  ("subproject_task_id", V, 0, 65535, 79), ("notes", V, 0, 65535, 144),
                  ("subproject_file", V, 0, 65535, 160), ("baseline_duration", F, 0, 74, 0),
                  ("baseline_duration_units", F, 0, 78, 0), ("baseline_start", F, 0, 104, 0),
                  ("baseline_finish", F, 0, 108, 0)),
    (12, "task"): (("unique_id", F, 0, 0, 86), ("id", F, 0, 4, 23), ("late_start", F, 0, 12, 39),
                   ("outline_level", F, 0, 40, 249), ("duration", F, 0, 60, 29), ("duration_units", F, 0, 64, 181),
                   ("constraint", F, 0, 80, 17), ("start", F, 0, 88, 35), ("finish", F, 0, 92, 36),
                   ("actual_start", F, 0, 96, 41), ("percent", F, 0, 122, 32), ("created", F, 0, 130, 93),
                   ("actual_finish", F, 0, 100, 42), ("late_finish", F, 0, 152, 40), ("wbs", V, 0, 65535, 16),
                   ("name", V, 0, 65535, 14), ("subproject_task_unique_id", V, 0, 65535, 242),
                   ("subproject_task_id", V, 0, 65535, 255), ("notes", V, 0, 65535, 15),
                   ("subproject_file", V, 0, 65535, 26), ("baseline_duration", F, 0, 74, 27),
                   ("baseline_duration_units", F, 0, 78, 179), ("baseline_start", F, 0, 104, 43),
                   ("baseline_finish", F, 0, 108, 44)),
    (14, "task"): (("unique_id", F, 0, 0, 86), ("id", F, 0, 4, 23), ("late_start", F, 0, 12, 39),
                   ("outline_level", F, 0, 40, 249), ("scheduled_duration", F, 0, 42, 29),
                   ("duration_units", F, 0, 46, 181), ("constraint", F, 0, 56, 17),
                   ("scheduled_start", F, 0, 64, 35), ("scheduled_finish", F, 0, 68, 36),
                   ("actual_start", F, 0, 72, 41), ("percent", F, 0, 90, 32), ("created", F, 0, 98, 93),
                   ("actual_finish", F, 0, 76, 42), ("late_finish", F, 0, 110, 40), ("wbs", V, 0, 65535, 16),
                   ("name", V, 0, 65535, 14), ("subproject_task_unique_id", V, 0, 65535, 242),
                   ("subproject_task_id", V, 0, 65535, 255), ("notes", V, 0, 65535, 15),
                   ("subproject_file", V, 0, 65535, 26), ("start", F, 1, 50, 1283),
                   ("finish", F, 1, 54, 1284), ("manual_duration", F, 1, 58, 1288),
                   ("manual_units", F, 1, 62, 1289), ("baseline_estimated_start", V, 0, 65535, 43),
                   ("baseline_estimated_finish", V, 0, 65535, 44), ("baseline_estimated_duration", V, 0, 65535, 27),
                   ("baseline_duration_units", V, 0, 65535, 179),
                   ("baseline_start", V, 1, 65535, 1299), ("baseline_finish", V, 1, 65535, 1300),
                   ("baseline_duration", V, 1, 65535, 1301)),
    (9, "resource"): (("unique_id", F, 0, 0, 0), ("id", F, 0, 4, 0), ("available_from", F, 0, 20, 0),
                      ("available_to", F, 0, 24, 0), ("max_units", F, 0, 44, 0), ("name", V, 0, 65535, 1),
                      ("availability", V, 0, 65535, 142)),
    (12, "resource"): (("unique_id", F, 0, 0, 27), ("id", F, 0, 4, 0), ("available_from", F, 0, 20, 57),
                       ("available_to", F, 0, 24, 58), ("max_units", F, 0, 44, 4), ("name", V, 0, 65535, 1),
                       ("availability", V, 0, 65535, 276)),
    (9, "assignment"): (("unique_id", F, 0, 0, 0), ("task_unique_id", F, 0, 4, 0),
                        ("resource_unique_id", F, 0, 8, 0), ("units", F, 0, 54, 0)),
    (12, "assignment"): (("unique_id", F, 0, 0, 0), ("task_unique_id", F, 0, 4, 1),
                         ("resource_unique_id", F, 0, 8, 2), ("units", F, 0, 54, 7)),
    (14, "assignment"): (("unique_id", F, 0, 0, 0), ("task_unique_id", F, 0, 4, 1),
                         ("resource_unique_id", F, 0, 8, 2), ("units", F, 0, 46, 7)),
}
DEFAULTS[(14, "resource")] = DEFAULTS[(12, "resource")]
# the Props keys of each field map (PropsKey *_FIELD_MAP, *_FIELD_MAP2), first found wins
MAP_KEYS = {"task": (131092, 50331668), "resource": (131093, 50331669), "assignment": (131095, 50331671)}
_BASE = {"task": TASK_BASE, "resource": RESOURCE_BASE, "assignment": ASSIGNMENT_BASE}


class BlockError(ValueError):
    """A VarMeta or FixedMeta without its magic number."""


def u8(b, o):
    return b[o] if b is not None and 0 <= o < len(b) else None


def u16(b, o):
    return int.from_bytes(b[o:o + 2], "little") if b is not None and 0 <= o and o + 2 <= len(b) else None


def i32(b, o):
    return int.from_bytes(b[o:o + 4], "little", signed=True) if b is not None and 0 <= o and o + 4 <= len(b) else None


def i64(b, o):
    return int.from_bytes(b[o:o + 8], "little", signed=True) if b is not None and 0 <= o and o + 8 <= len(b) else None


def f64(b, o):
    """MPPUtility.getDouble: NaN is 0."""
    if b is None or o < 0 or o + 8 > len(b):
        return None
    v = struct.unpack_from("<d", b, o)[0]
    return 0.0 if math.isnan(v) else v


def timestamp(b, o):
    """MPPUtility.getTimestamp: u16 tenths... of six seconds, then u16 days since 1983-12-31 (0, 1 and 65535 none; a
    day below 100 with seconds is none too)."""
    days, time = u16(b, o + 2), u16(b, o)
    if days is None or days <= 1 or days == 65535:
        return None
    t = EPOCH + timedelta(days=days, seconds=(0 if time == 65535 else time) * 6)
    return None if days < 100 and t.second else t


def unicode(b, o=0):
    """MPPUtility.getUnicodeString: UTF-16LE up to the first aligned NUL pair."""
    if b is None or o >= len(b):
        return ""
    end = len(b)
    for k in range(o, len(b) - 1, 2):
        if b[k] == 0 and b[k + 1] == 0:
            end = k
            break
    return b[o:end].decode("utf-16-le", "replace")


def string8(b, o=0):
    """MPPUtility.getString: 8-bit characters up to the first NUL (Latin-1 here; MPXJ widens them as signed)."""
    if b is None:
        return None
    end = b.find(b"\0", o)
    return b[o:len(b) if end < 0 else end].decode("latin-1")


def duration_units(v, default="DAYS"):
    """MPPUtility.getDurationTimeUnits: 21 is the project's default, an unknown value DAYS."""
    if v is None:
        return None
    v &= 0x1F
    return (default or "DAYS") if v == 21 else _UNITS.get(v, "DAYS")


def duration(v, unit):
    """MPPUtility.getDuration: (amount, unit) from tenths of a minute."""
    return v / _TENTHS[unit] if unit in _TENTHS else float(v), unit


def adjusted(props, v, unit):
    """MPPUtility.getAdjustedDuration: days, weeks and months by the project's minutes a day and a week and days a
    month (0 when that is 0); -1 is none."""
    if v is None or v == -1:
        return None
    per = {"DAYS": props.minutes_per_day * 10, "ELAPSED_DAYS": 24 * 600, "WEEKS": props.minutes_per_week * 10,
           "ELAPSED_WEEKS": 60 * 24 * 7 * 10, "MONTHS": props.minutes_per_day * props.days_per_month * 10,
           "ELAPSED_MONTHS": 60 * 24 * 30 * 10}
    if unit in per:
        return (v / per[unit] if per[unit] else 0.0), unit
    return duration(v, unit)


def percentage(b, o):
    v = u16(b, o)
    return float(v) if v is not None and v <= 100 else None


def units(v):
    """A CURRENCY or UNITS double: below 0.1 is 0, then a hundredth."""
    if v is None:
        return None
    return (0.0 if abs(v) < 0.1 else v) / 100


def props(data: bytes) -> dict:
    """Props9/12/14: {key: bytes} from a 16-byte header (u16 count at 12) and (i32 size, i32 key, i32) items, each
    padded to two bytes; a size below 1 or past the data ends them."""
    out, pos, found = {}, 16, 0
    count = u16(data, 12) or 0
    while found < count and len(data) - pos >= 12:
        size, key = i32(data, pos), i32(data, pos + 4)
        pos += 12
        if len(data) - pos < size or size < 1:
            break
        out[key] = data[pos:pos + size]
        pos += size + (size & 1)
        found += 1
    return out


class VarMeta:
    """VarMeta9 (3-byte unique id, byte type, i32 offset) or VarMeta12 (i32 unique id, i32 offset, u16 type, two
    bytes): {unique id: {type: offset}} and the offsets."""

    def __init__(self, data: bytes, v12: bool):
        magic = int.from_bytes(data[:4], "little") if len(data) >= 4 else None
        if magic != MAGIC and not (v12 and magic == 0):
            raise BlockError("VarMeta magic")
        count = i32(data, 8) or 0
        self.table, self.offsets = {}, []
        pos, size = 24, 12 if v12 else 8
        for _ in range(max(count, 0)):
            if pos + size > len(data):
                break
            if v12:
                uid, off, kind = i32(data, pos), i32(data, pos + 4), u16(data, pos + 8)
            else:
                uid, kind, off = int.from_bytes(data[pos:pos + 3], "little"), data[pos + 3], i32(data, pos + 4)
            self.table.setdefault(uid, {})[kind] = off
            self.offsets.append(off)
            pos += size
        self.offsets.sort()

    def types(self, uid) -> dict:
        return self.table.get(uid, {})

    def offset(self, uid, kind):
        return None if kind is None else self.table.get(uid, {}).get(kind)


class Var2Data:
    """Each item VarMeta names: an i32 size then its bytes (a size below 0 or past the stream: left out)."""

    def __init__(self, meta: VarMeta, data: bytes):
        self.meta, self.items = meta, {}
        for off in meta.offsets:
            if off < 0 or off >= len(data) or off in self.items:
                continue
            size = i32(data, off)
            if size is None or size < 0 or size > len(data) - off - 4:
                continue
            self.items[off] = data[off + 4:off + 4 + size]

    def get(self, uid, kind):
        off = self.meta.offset(uid, kind)
        return None if off is None else self.items.get(off)

    def short(self, uid, kind) -> int:
        b = self.get(uid, kind)
        return u16(b, 0) if b is not None and len(b) >= 2 else 0

    def int(self, uid, kind) -> int:
        b = self.get(uid, kind)
        return i32(b, 0) if b is not None and len(b) >= 4 else 0

    def text(self, uid, kind):
        b = self.get(uid, kind)
        return None if b is None else unicode(b, 0)


class FixedMeta:
    """A 16-byte header (magic, i32 item count at 8) and items of one size: the size given, or the first of `sizes`
    that divides the stream into as many items as `other` (a FixedData) holds, else the one whose items come nearest
    to the count without passing the stream (FixedMeta's item size provider)."""

    def __init__(self, data: bytes, size: int = 0, other=None, sizes=()):
        if len(data) < 16 or int.from_bytes(data[:4], "little") != MAGIC:
            raise BlockError("FixedMeta magic")
        self.count = i32(data, 8)
        available = len(data) - 16
        if sizes:
            size, distance = sizes[0], -2 ** 31
            for test in sizes:
                if available % test == 0:
                    if available // test == len(other.items):
                        size = test
                        break
                    d = self.count * test - available
                    if distance < d <= 0:
                        size, distance = test, d
        self.items = [data[16 + k * size:16 + (k + 1) * size] for k in range(available // size)]

    def item(self, k):
        return self.items[k] if 0 <= k < len(self.items) else None


class FixedData:
    """Items cut from a stream: by the offsets in a FixedMeta's items (i32 at 4), each up to the next item's offset
    (`max_size` caps it, a 0 size becomes `min_size`), or one `size` each from those offsets (`by_meta_size`), or in
    `size` steps with no meta (`blocks`)."""

    def __init__(self, items, offsets):
        self.items, self.offsets = items, offsets

    @classmethod
    def by_meta(cls, meta: FixedMeta, data: bytes, max_size: int = 0, min_size: int = 0):
        n = len(meta.items)
        items, offsets = [None] * n, [0] * n
        for k in range(n):
            at = i32(meta.items[k], 4)
            if at is None or at < 0 or at > len(data):
                continue
            nxt = i32(meta.items[k + 1], 4) if k + 1 < n else None
            size = len(data) - at if k + 1 == n else (nxt - at if nxt is not None else -1)
            if size == 0:
                size = min_size
            available = len(data) - at
            if size < 0 or size > available:
                size = available if max_size == 0 else min(max_size, available)
            if max_size and size > max_size:
                size = max_size
            if size > 0:
                items[k], offsets[k] = data[at:at + size], at
        return cls(items, offsets)

    @classmethod
    def by_meta_size(cls, meta: FixedMeta, size: int, data: bytes):
        """Each item `size` bytes at its offset; an item past the end shortens `size` for the items after it, as
        MPXJ's FixedData does."""
        n = len(meta.items)
        items, offsets = [None] * n, [0] * n
        for k in range(n):
            at = i32(meta.items[k], 4)
            if at is None or at < 0 or at > len(data):
                continue
            available = len(data) - at
            if size < 0 or size > available:
                size = available
            items[k], offsets[k] = data[at:at + size], at
        return cls(items, offsets)

    @classmethod
    def blocks(cls, size: int, data: bytes):
        n = len(data) // size
        return cls([data[k * size:(k + 1) * size] for k in range(n)], [k * size for k in range(n)])

    def item(self, k):
        return self.items[k] if k is not None and 0 <= k < len(self.items) else None

    def index_of(self, offset) -> int:
        try:
            return self.offsets.index(offset)
        except ValueError:
            return -1


class FieldMap:
    """The fields of one kind ("task", "resource", "assignment") of an MPP `version` (9, 12 or 14): {field: (location,
    block, offset, var key)} from the project Props' field map, else the version's defaults, and each block's `max`."""

    def __init__(self, version: int, kind: str, project_props: dict, project):
        self.version, self.project, self.items, self.max = version, project, {}, [0, 0]
        names = {"task": _TASK14 if version == 14 else _TASK, "resource": _RESOURCE, "assignment": _ASSIGNMENT}[kind]
        self.types = {f[0]: f[1:] for f in names.values() if f}
        data = next((project_props[k] for k in MAP_KEYS[kind] if k in project_props), None)
        if data is None:
            for name, loc, block, off, key in DEFAULTS[(version, kind)]:
                self.items[name] = (loc, block, off, key)
        else:
            self._read(data, names, _BASE[kind])

    def _read(self, data: bytes, names: dict, base: int):
        last, block = 0, 0
        for at in range(0, len(data) - 21, 28):
            off, field_id, category = u16(data, at + 4), i32(data, at + 12) & 0xFFFFFFFF, u16(data, at + 20)
            key = field_id & 0xFFFF if self.version != 9 else data[at + 6]
            prefix, index = field_id & 0xFFFF0000, field_id & 0xFFFF
            name = names.get(index) if prefix == base and not index & 0x8000 else None
            if category in (0x0B, 0x64):
                loc = META
            elif off != 65535:
                loc = FIXED
                if off < last:
                    block += 1
                last = off
                sizes = _SIZES.get(prefix, "")
                size = _SIZE_CODES[sizes[index]] if not index & 0x8000 and index < len(sizes) else 0
                if block < 2:
                    self.max[block] = max(self.max[block], off + size)
            else:
                loc = VAR if key else UNKNOWN
            if name:
                self.items[name[0]] = (loc, block, off, key)

    def offset(self, name: str) -> int:
        """The field's fixed data offset, -1 when the map has no such field."""
        it = self.items.get(name)
        return -1 if it is None else it[2]

    def key(self, name: str):
        it = self.items.get(name)
        return None if it is None else it[3]

    def read_all(self, uid, blocks, var) -> dict:
        """{field: value} of every field the map places (MPXJ's populateContainer)."""
        return {name: self.read(name, uid, blocks, var) for name in self.items}

    def read(self, name, uid, blocks, var):
        it = self.items.get(name)
        if it is None:
            return None
        loc, block, off, key = it
        kind, *units_field = self.types[name]
        if loc == FIXED:
            data = blocks[block] if block < len(blocks) else None
            return self._fixed(kind, units_field, data, off, uid, blocks, var) if data and off < len(data) else None
        if loc == VAR:
            return self._var(kind, units_field, uid, key, blocks, var)
        return None

    def _units(self, units_field, uid, blocks, var):
        return self.read(units_field[0], uid, blocks, var) if units_field else None

    def _fixed(self, kind, units_field, data, off, uid, blocks, var):
        p = self.project
        if kind == "DATE":
            return timestamp(data, off)
        if kind == "INTEGER":
            return i32(data, off)
        if kind == "DURATION":
            unit = self._units(units_field, uid, blocks, var) or p.duration_units
            return adjusted(p, i32(data, off), unit)
        if kind == "TIME_UNITS":
            return duration_units(u16(data, off), p.duration_units)
        if kind == "CONSTRAINT":
            v = u16(data, off)
            return None if v is None else CONSTRAINTS[v] if v < len(CONSTRAINTS) else CONSTRAINTS[0]
        if kind == "PERCENTAGE":
            return percentage(data, off)
        if kind == "SHORT":
            return u16(data, off)
        if kind == "BOOLEAN":
            v = u16(data, off)
            return None if v is None else v != 0
        if kind == "UNITS":
            return units(f64(data, off))
        return None

    def _var(self, kind, units_field, uid, key, blocks, var):
        """A var data value; None for a string or date held in a custom field's value list, which is not read."""
        if kind == "DURATION":
            unit = self._units(units_field, uid, blocks, var) or "HOURS"
            b = var.get(uid, key)
            if b is None:
                return None
            return unicode(b, 0) if len(b) == 512 else adjusted(self.project, i32(b, 0), unit) if len(b) >= 4 else None
        if kind == "TIME_UNITS":
            return duration_units(var.short(uid, key), self.project.duration_units)
        if kind in ("STRING", "DATE"):
            if var.short(uid, key) in VALUE_LISTS:
                return None
            b = var.get(uid, key)
            if kind == "STRING":
                return None if b is None else unicode(b, 0)
            return None if b is None else unicode(b, 0) if len(b) == 512 else timestamp(b, 0) if len(b) >= 4 else None
        if kind == "INTEGER":
            return var.int(uid, key)
        if kind == "BINARY":
            return var.get(uid, key)
        if kind == "NOTES":
            return string8(var.get(uid, key))
        if kind in ("PERCENTAGE", "SHORT"):
            return var.short(uid, key)
        if kind == "BOOLEAN":
            return var.short(uid, key) != 0
        return None
