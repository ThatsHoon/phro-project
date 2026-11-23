# Ported from MPXJ (https://www.mpxj.org), copyright (c) Packwood Software, under the GNU Lesser General Public
# License 2.1 or later. This module is distributed under the same license: see https://www.gnu.org/licenses/lgpl-2.1.
"""Microsoft Project 98 to 2019 (MPP8, MPP9, MPP12, MPP14): the tasks, resources, assignments and relations of an
open compound file, as MPXJ's MPPReader reads them. A port of org.mpxj.mpp MPPReader, CompObj, MPP9Reader,
MPP12Reader, MPP14Reader, ProjectPropertiesReader, ConstraintFactory, ResourceAssignmentFactory, AvailabilityFactory,
and of the parts of ProjectFile, TaskContainer, Task, Resource and RelationContainer they rely on
(the ID and unique ID maps, updateStructure, the WBS and outline numbers, predecessor de-duplication).

`read` returns a Project; `mpp.build` turns it into the Doc. A task's baseline duration, start and finish are read
(an empty one from its estimated baseline, as MPPReader's copyEstimatedBaselineFields does); Baselines 1-10, baseline
cost and work, calendars, custom fields, views, time-phased data, recurring tasks and hyperlinks are not read; nor are enterprise columns or unique ID clashes (MPXJ renumbers
clashing unique IDs at the end, which no output shows). A Project password set to read the file is `encrypted`; a
read MPXJ fails with an exception (a value past the end of its block, a missing stream, a bad magic number, a task
order it cannot fix) is `damaged`.
"""
import re
from datetime import datetime, timedelta

from ..limits import ReadError
from . import doc_fib
from . import mpp_blocks as B
from .mpp_blocks import FieldMap, FixedData, FixedMeta, Var2Data, VarMeta

PASSWORD = "Project password set"
DAMAGED = "the Project data could not be read"
COMP_OBJ = "\x01CompObj"
FORMATS = {"MSProject.MPP9": 9, "MSProject.MPT9": 9, "MSProject.GLOBAL9": 9, "MSProject.MPP8": 8,
           "MSProject.MPT8": 8, "MSProject.MPP12": 12, "MSProject.MPT12": 12, "MSProject.GLOBAL12": 12,
           "MSProject.MPP14": 14, "MSProject.MPT14": 14, "MSProject.GLOBAL14": 14}
PROJECT_DIR = {9: "   19", 12: "   112", 14: "   114"}
APP_2010, APP_2013 = 14, 15
NULL_RESOURCE = -65535
MAX_UNIQUE_ID = 0x1FFFFF
START_NA, END_NA = datetime(1984, 1, 1), datetime(2049, 12, 31, 23, 59)
RELATION_TYPES = ("FINISH_FINISH", "FINISH_START", "START_FINISH", "START_START")
ALAP, SNLT, FNLT = "AS_LATE_AS_POSSIBLE", "START_NO_LATER_THAN", "FINISH_NO_LATER_THAN"
# PropsKey
K_START, K_FINISH, K_STATUS, K_TITLE = 37748738, 37748739, 37748805, 37748744
K_UNITS, K_MIN_DAY, K_MIN_WEEK, K_DAYS_MONTH = 37748757, 37748765, 37748766, 37753743
K_PASSWORD_FLAG, K_PASSWORD_HASH, K_ENCRYPTION = 893386752, 893386756, 893386759
K_SUBPROJECTS = 37748898
# the subproject unique ID types that name the subproject's own task (MPPnReader SUBPROJECT_TASKUNIQUEIDn)
_OWN = {9: (0, 0x0B340000, 0x0ABB0000, 0x05A10000, 0x02F70000, 0x07010000),
        12: (0, 0x0B340000, 0x0ABB0000, 0x05A10000, 0x0BD50000, 0x03D60000, 0x07010000),
        14: (0, 0x0B340000, 0x0ABB0000, 0x05A10000, 0x0BD50000, 0x03D60000, 0x067F0000, 0x067D0000, 0x00540000)}
_PATH_UNICODE = 0x0BD50000
_LIST_END = 0x303
# processSubProjectData: item type -> (offset reads, how many subprojects); u/p/n an int & 0x1FFFF, U/P/N a short,
# each 4 bytes on, s 4 bytes skipped, 8 a short then 8 bytes on, h the unique IDs at the item header, - none
_I, _S8 = "upn", "UPN"
_SUB14 = {}
for _k, _v in (((0x00, 0x10, 0x02), ("ss", None)), ((0x0B, 0x99, 0x09, 0x0D), ("uspn", "all")),
               ((0x03, 0x11, 0x91), ("upns", "all")), ((0x81, 0x83, 0x41), ("upsn", "all")),
               ((0x01, 0x08), ("upn", "all")), ((0xC0,), ("hpns", "all")), ((0x05,), ("upn", "one")),
               ((0x45,), ("upns", "one")), ((0x04,), ("-pn", "one")), ((0x89, 0x8D), ("U8P8N", "all")),
               ((0x0A,), ("UPN", "all")), ((0x44,), ("-psn", "all")), ((0x80,), ("sss", None))):
    for _t in _k:
        _SUB14[_t] = _v
_SUB9 = {}
for _k, _v in (((0x00, 0x10), ("ss", None)), ((0x99, 0x09, 0x0D), ("UsPN", "one")), ((0x91,), ("UPNs", "one")),
               ((0x01, 0x03, 0x05, 0x08, 0x0A, 0x11), ("UPN", "one")), ((0x81, 0x41), ("UPsN", "one")),
               ((0xC0,), ("hPNs", "one")), ((0x45,), ("upns", "one")), ((0x02, 0x04), ("-PN", "one")),
               ((0x8D,), ("U8P8N", "one")), ((0x80,), ("sss", None)), ((0x44,), ("-PsN", "one"))):
    for _t in _k:
        _SUB9[_t] = _v
# minutes in one unit, for comparing lags (Duration.convertUnits at 480 minutes a day, 2400 a week, 20 days a month)
_MINUTES = {"MINUTES": 1, "HOURS": 60, "DAYS": 480, "WEEKS": 2400, "MONTHS": 9600, "YEARS": 115200,
            "ELAPSED_MINUTES": 1, "ELAPSED_HOURS": 60, "ELAPSED_DAYS": 1440, "ELAPSED_WEEKS": 10080,
            "ELAPSED_MONTHS": 43200, "ELAPSED_YEARS": 525600}


class Unfixable(Exception):
    """MPXJ's "Unable to fix task order"."""


def get_byte(b, o):
    if b is None or not 0 <= o < len(b):
        raise IndexError(o)
    return b[o]


def get_short(b, o):
    if b is None or o < 0 or o + 2 > len(b):
        raise IndexError(o)
    return b[o] | b[o + 1] << 8


def get_int(b, o):
    if b is None or o < 0 or o + 4 > len(b):
        raise IndexError(o)
    return int.from_bytes(b[o:o + 4], "little", signed=True)


def get_long(b, o):
    if b is None or o < 0 or o + 8 > len(b):
        raise IndexError(o)
    return int.from_bytes(b[o:o + 8], "little", signed=True)


def get_double(b, o):
    get_long(b, o)
    return B.f64(b, o)


def _string8(b, o):
    """MPPUtility.getString: to the first NUL or the end."""
    if o < 0:
        raise IndexError(o)
    return B.string8(b, o) if o < len(b) else ""


def _unicode(b, o, limit=0):
    """MPPUtility.getUnicodeString with a byte limit (0: none)."""
    if o >= len(b):
        return ""
    s = B.unicode(b, o)
    if limit > 0 and len(s) * 2 > limit:
        s = b[o:o + limit].decode("utf-16-le", "replace")
    return s


def _cmp(a, b) -> int:
    """LocalDateTimeHelper.compare: a null date is after every date."""
    if a is None or b is None:
        return 0 if a is b else 1 if a is None else -1
    return (a > b) - (a < b)


def _same(a, b) -> bool:
    """Duration.compareTo(b) == 0: b in a's units, equal within 1e-5."""
    amount = b[0]
    if a[1] != b[1]:
        amount = b[0] * _MINUTES.get(b[1], 1) / _MINUTES.get(a[1], 1)
    return abs(a[0] - amount) < 0.00001


class Task:
    """A task: its fields by name (`f`), as MPXJ caches them."""

    def __init__(self):
        self.f, self.null, self.summary = {}, False, False
        self.parent, self.children, self.outline_number = None, [], None

    @property
    def id(self):
        return self.f.get("id")

    @property
    def uid(self):
        return self.f.get("unique_id")

    @property
    def active(self) -> bool:
        return self.f.get("active") is not False

    @property
    def milestone(self) -> bool:
        return bool(self.f.get("milestone"))


class Resource:
    def __init__(self):
        self.f, self.kind, self.max_units = {}, "WORK", None

    @property
    def uid(self):
        return self.f.get("unique_id")

    @property
    def active(self) -> bool:
        return self.f.get("active") is not False


class Assignment:
    def __init__(self, resource_uid):
        self.resource_uid, self.units = resource_uid, 100.0


class Relation:
    def __init__(self, pred, kind, lag):
        self.pred, self.kind, self.lag = pred, kind, lag or (0.0, "DAYS")


class Project:
    """ProjectFile: the tasks with their ID and unique ID maps, the resources, the assignments by task unique ID and
    the predecessors by successor task; and the project properties. `baseline`: whether a task that is not null has
    a baseline start or finish."""

    def __init__(self):
        self.tasks, self.ids, self.uids = [], {}, {}
        self.resources, self.resource_uids = [], {}
        self.assigned, self.preds = {}, {}
        self.title = self.start = self.finish = self.status = None
        self.duration_units, self.minutes_per_day, self.minutes_per_week, self.days_per_month = "DAYS", 0, 0, 0
        self.auto_wbs = self.baseline = False

    def add_task(self) -> Task:
        t = Task()
        self.tasks.append(t)
        return t

    def remove_task(self, t: Task):
        self.tasks.remove(t)
        self.uids.pop(t.uid, None)
        self.ids.pop(t.id, None)
        self.assigned.pop(t.uid, None)

    def set_uid(self, entity, value, uids=None):
        """Task.setUniqueID or populating it (ProjectEntityContainer.updateUniqueID on a change)."""
        uids = self.uids if uids is None else uids
        old = entity.f.get("unique_id")
        entity.f["unique_id"] = value
        if old == value:
            return
        if old is not None:
            uids.pop(old, None)
        if uids.get(value) is not entity:
            uids[value] = entity

    def set_id(self, t: Task, value):
        """Task.setID: the previous ID is unmapped, whatever task it maps."""
        if t.id is not None:
            self.ids.pop(t.id, None)
        self.ids[value] = t
        t.f["id"] = value

    def populate(self, entity, values: dict, uids=None):
        for name, value in values.items():
            if name == "unique_id":
                self.set_uid(entity, value, uids)
            else:
                entity.f[name] = value

    def predecessors(self, t: Task) -> list:
        return self.preds.get(t, [])

    def assignments(self, t: Task) -> list:
        return self.assigned.get(t.uid, [])

    def add_predecessor(self, succ: Task, pred: Task, kind: str, lag):
        """RelationContainer.addPredecessor: the first relation from `pred` stops the search; it is kept instead
        when its type and lag are the same."""
        rels = self.preds.setdefault(succ, [])
        new = Relation(pred, kind, lag)
        for r in rels:
            if r.pred is pred:
                if r.kind == kind and _same(r.lag, new.lag):
                    return
                break
        rels.append(new)

    def remove_predecessor(self, succ: Task, rel: Relation):
        for r in self.preds.get(succ, []):
            if r.pred is rel.pred and r.kind == rel.kind and _same(r.lag, rel.lag):
                self.preds[succ].remove(r)
                return


def compobj(data: bytes) -> tuple:
    """CompObj: (application name, file format) after the 28-byte header, each an i32 length and its bytes less the
    last; the format of "Microsoft Project 4.0" is MSProject.MPP4. ValueError when they do not fit."""
    n = get_int(data, 28)
    if n < 1 or 32 + n > len(data):
        raise ValueError("CompObj")
    name = data[32:32 + n - 1].decode("latin-1")
    if name == "Microsoft Project 4.0":
        return name, "MSProject.MPP4"
    at = 32 + n
    m = get_int(data, at)
    if m <= 0:
        return name, None
    if at + 4 + m > len(data):
        raise ValueError("CompObj")
    return name, data[at + 4:at + 4 + m - 1].decode("latin-1")


def read(cf) -> Project:
    """The Project of the open compound file `cf`, whose CompObj names Microsoft Project."""
    try:
        name, fmt = compobj(cf.read(COMP_OBJ))
        version = FORMATS.get(fmt)
        if version is None:
            raise ValueError("not a Project format MPXJ reads")
        m = re.fullmatch(r"Microsoft.Project.(\d+)\.0", name)
        app = int(m.group(1)) if m else 0
        if version == 8:
            from . import mpp8
            project = mpp8.read(cf)
        else:
            project = _Reader(cf, version, app).run()
        finish(project)
        return project
    except ReadError:
        raise
    except (IndexError, KeyError, ValueError, Unfixable):
        raise ReadError("damaged", DAMAGED) from None


class _Reader:
    """MPP9Reader, MPP12Reader and MPP14Reader."""

    def __init__(self, cf, version: int, app: int):
        self.cf, self.v, self.app = cf, version, app
        self.p = Project()
        self.dir = PROJECT_DIR[version] + "/"
        self.sub, self.external = {}, set()
        self.order, self.null_order = {}, {}
        self.null_size = 8 if version == 9 else 16

    def stream(self, name):
        return self.cf.read(self.dir + name)

    def coded(self, name):
        """DocumentInputStreamFactory: the stream XORed with the encryption code when the password flag is set."""
        data = self.stream(name)
        return bytes(x ^ self.mask for x in data) if self.encrypted and self.mask else data

    def run(self) -> Project:
        v = self.v
        root = B.props(self.cf.read(f"Props{v}"))
        flag = root.get(K_PASSWORD_FLAG, b"\0")[:1] or b"\0"
        code = (root.get(K_ENCRYPTION, b"\0")[:1] or b"\0")[0]
        self.encrypted, self.mask = flag[0] != 0, 0 if code == 0 else 0xFF - code
        if flag[0] & 1 and self._password(root):
            raise ReadError("encrypted", PASSWORD)
        outline = "TBkndOutlCode/"
        VarMeta(self.stream(outline + "VarMeta"), v != 9)
        if v != 9:
            FixedMeta(self.stream(outline + "FixedMeta"), 10)
            FixedMeta(self.stream(outline + "Fixed2Meta"), 10)
        self.pp = B.props(self.coded("Props"))
        properties(self.p, self.pp, self.cf)
        self.subprojects()
        self.resources()
        self.tasks()
        self.relations()
        self.assignments()
        post_process(self.p, self.order, self.null_order)
        return self.p

    def _password(self, root) -> bool:
        if self.v == 14:
            return K_PASSWORD_HASH in root
        if self.v == 12:
            return True
        data = root.get(K_PASSWORD_HASH)
        if data is None:
            raise ValueError("password hash")
        return len(data) >= 64 and data[60] ^ self.mask != 0

    def subprojects(self):
        data = self.pp.get(K_SUBPROJECTS)
        if data is None:
            return
        v9, table = self.v == 9, _SUB9 if self.v == 9 else _SUB14
        off, end = 12, get_int(data, 8)
        while off < end:
            header = get_short(data, off) if v9 else get_int(data, off) & 0x1FFFF
            off += 4
            ops, how = table.get(get_byte(data, header + 16), ("sss", None))
            got = {"u": -1}
            for op in ops:
                if op in _I:
                    got[op] = get_int(data, off) & 0x1FFFF
                    off += 4
                elif op in _S8:
                    got[op.lower()] = get_short(data, off)
                    off += 4
                elif op == "8":
                    off += 4
                elif op == "s":
                    off += 4
                elif op == "h":
                    got["u"] = header
            if how == "one":
                self.subproject(data, got["u"], got["p"])
            elif how == "all":
                uid = got["u"]
                while uid < got["p"]:
                    self.subproject(data, uid, got["p"])
                    uid += 4

    def subproject(self, data, uid_off, path):
        try:
            kind = 0 if uid_off == -1 or self.v == 9 else get_int(data, uid_off + 4)
            if kind == _PATH_UNICODE:
                sp = _unicode(data, path)
            else:
                path += 22
                dos = _string8(data, path)
                path += len(dos) + 1 + 24
                size = get_int(data, path)
                path += 4
                if size == 0:
                    sp = dos
                else:
                    size = get_int(data, path)
                    path += 6
                    sp = _unicode(data, path, size)
            self.unique_ids(sp, data, uid_off)
        except IndexError:
            pass

    def unique_ids(self, sp, data, off):
        if off == -1:
            return
        own = _OWN[self.v]
        if self.v != 9:
            value, kind = get_int(data, off), get_int(data, off + 4)
            if value == 0 or value > MAX_UNIQUE_ID:
                return
            if kind not in own:
                self.external.add(value)
            self.sub[value] = sp
            return
        prev, value = 0, get_int(data, off)
        while value != _LIST_END:
            if value in own:
                self.sub[prev] = sp
                prev = 0
            else:
                if prev != 0:
                    self.external.add(prev)
                    self.sub[prev] = sp
                prev = value
            off += 4
            value = get_int(data, off)
        if prev != 0:
            self.external.add(prev)
            self.sub[prev] = sp

    def resources(self):
        v, p = self.v, self.p
        fm = FieldMap(v, "resource", self.pp, p)
        d = "TBkndRsc/"
        vm = VarMeta(self.stream(d + "VarMeta"), v != 9)
        vd = Var2Data(vm, self.stream(d + "Var2Data"))
        meta = FixedMeta(self.stream(d + "FixedMeta"), 37)
        fd = FixedData.by_meta(meta, self.coded(d + "FixedData"))
        meta2 = fd2 = None
        if v == 14:
            meta2 = FixedMeta(self.stream(d + "Fixed2Meta"), other=fd, sizes=(50, 51))
        elif v == 12:
            meta2 = FixedMeta(self.stream(d + "Fixed2Meta"), 49)
        if meta2 is not None:
            fd2 = FixedData.by_meta(meta2, self.coded(d + "Fixed2Data"))
        found = {}
        uoff = fm.offset("unique_id") if v == 14 else 0
        for k in range(len(meta.items)):
            data = fd.item(k)
            if data is None or len(data) < fm.max[0]:
                continue
            if v == 9:
                if get_int(meta.item(k), 0) & 2:
                    continue
                found[get_short(data, 0)] = k
            else:
                found.setdefault(get_short(data, uoff), k)
        now = datetime.now()
        for uid in sorted(vm.table):
            k = found.get(uid)
            if k is None:
                continue
            data = fd.item(k)
            m2 = meta2.item(k) if meta2 else None
            r = Resource()
            p.resources.append(r)
            p.populate(r, fm.read_all(uid, [data, fd2.item(k) if fd2 else None], vd), p.resource_uids)
            r.f["id"] = get_int(data, fm.offset("id") if v == 14 else 4)
            p.set_uid(r, uid, p.resource_uids)
            r.max_units = max_units(r, now)
            m = meta.item(k)
            if v == 12:
                get_byte(m2, 8)
            if v == 14 and m2 is not None:
                get_int(m2, 16), get_short(m2, 48)
            work = get_byte(m, 12) & 0x10 if v == 14 and self.app > APP_2010 else get_byte(m, 9) & 0x02
            if work:
                r.kind = "WORK"
            elif v == 9:
                r.kind = "MATERIAL"
            else:
                r.kind = "COST" if get_byte(m2, 8) & 0x10 else "MATERIAL"

    def task_map(self, fm, meta, fd, fd2, vm) -> dict:
        out, uoff, mx = {}, fm.offset("unique_id"), fm.max[0]
        for k in range(len(meta.items) - 1, 2, -1):
            data = fd.item(k)
            if data is None or (fd2 is not None and fd2.item(k) is None):
                continue
            flags = get_int(meta.item(k), 0)
            if flags & 2:
                key = get_short(data, 0)
                if self.v != 14 or key not in out:
                    out[key] = None
            elif len(data) == self.null_size:
                out.setdefault(get_int(data, 0), k)
            elif mx == 0 or len(data) * 100 // mx > 75:
                key = get_int(data, uoff)
                if (key not in out or vm.types(key)) and (key not in out or not flags & 4):
                    out[key] = k
        return out

    def null_task(self, uid, tid):
        t = self.p.add_task()
        t.null = True
        self.p.set_uid(t, uid)
        self.p.set_id(t, tid)
        self.null_order[t.id] = t.uid

    def tasks(self):
        v, p = self.v, self.p
        fm = FieldMap(v, "task", self.pp, p)
        d = "TBkndTask/"
        vm = VarMeta(self.stream(d + "VarMeta"), v != 9)
        vd = Var2Data(vm, self.stream(d + "Var2Data"))
        meta = FixedMeta(self.stream(d + "FixedMeta"), 47)
        mx = fm.max[0]
        if v == 14:
            fd = FixedData.by_meta(meta, self.stream(d + "FixedData"), mx)
            meta2 = FixedMeta(self.stream(d + "Fixed2Meta"), other=fd, sizes=(92, 93, 94, 95, 96))
        elif v == 12:
            fd = FixedData.by_meta(meta, self.stream(d + "FixedData"), 768, mx)
            meta2 = FixedMeta(self.stream(d + "Fixed2Meta"), 86)
        else:
            fd = FixedData.by_meta(meta, self.coded(d + "FixedData"), 768, mx)
            meta2 = None
        fd2 = FixedData.by_meta(meta2, self.stream(d + "Fixed2Data")) if meta2 else None
        tmap = self.task_map(fm, meta, fd, fd2, vm)
        new_bits = self.app > APP_2010
        auto_wbs, external = True, []
        for uid in sorted(tmap):
            k = tmap[uid]
            if k is None or not 0 <= k < len(fd.items):
                continue
            data = fd.items[k]
            if data is None:
                raise IndexError(k)
            tid = get_int(data, fm.offset("id"))
            if len(data) == self.null_size:
                if v == 9:
                    self.null_task(get_short(data, 0), get_short(data, 4))
                    continue
                nid = get_int(data, 4) if v == 14 else get_short(data, 4)
                if nid not in self.null_order:
                    self.null_task(get_int(data, 0) if v == 14 else get_short(data, 0), nid)
                continue
            if len(data) < mx:
                if v == 9 or (v == 12 and uid != 0):
                    continue
                data = data + bytes(mx + 8 - len(data))
            if v == 9 and uid != 0 and uid not in vm.table:
                continue
            m = meta.item(k)
            m2, data2 = (meta2.item(k), fd2.item(k)) if meta2 else (None, None)
            temp = p.ids.get(tid)
            if temp is not None:
                if uid not in vm.table:
                    continue
                if temp.f.get("name") is None:
                    p.remove_task(temp)
            t = p.add_task()
            p.populate(t, fm.read_all(uid, [data, data2] if meta2 else [data], vd))
            f = t.f
            if v != 14:
                get_byte(m, 13), get_byte(m, 12), get_byte(m, 11), get_byte(m, 10)
            if (f.get("subproject_task_id") or 0) != 0:
                f["external_task"] = True
                external.append(t)
            p.set_id(t, tid)
            if v == 12:
                get_byte(m, 40)
            if v != 14:
                f["milestone"] = get_byte(m, 8) & 0x20 != 0
            p.set_uid(t, uid)
            manual = False
            if v == 14:
                f["milestone"] = (get_int(m, 10) & 0x02 if new_bits else get_int(m, 8) & 0x20) != 0
                if m2 is not None:
                    f["active"] = (get_int(m2, 8) & (0x40 if new_bits else 0x04)) != 0
                    f["task_mode"] = (get_int(m2, 8) & (0x80 if new_bits else 0x08)) != 0
                manual = bool(f.get("task_mode"))
                for name in ("start", "finish", "duration"):
                    sched = f.get("scheduled_" + name)
                    if f.get(name) is None or (sched is not None and not manual):
                        f[name] = sched
            constraint = f.get("constraint")
            if constraint is None:
                raise ValueError("no constraint type")
            if constraint == ALAP:
                if _cmp(f.get("start"), f.get("late_start")) < 0:
                    f["start"] = f.get("late_start")
                if _cmp(f.get("finish"), f.get("late_finish")) < 0:
                    f["finish"] = f.get("late_finish")
            elif constraint in (SNLT, FNLT) and _cmp(f.get("finish"), f.get("start")) < 0:
                f["finish"] = f.get("late_finish")
            sp = self.sub.get(t.uid)
            if sp is not None:
                f["subproject_file"] = sp
                if f.get("subproject_task_unique_id") is not None:
                    f["subproject_task_unique_id"] &= 0xFFFF
            if t.uid in self.external and (f.get("subproject_task_unique_id") or 0) != 0:
                f["external_task"] = True
            if f.get("wbs") is not None:
                auto_wbs = False
            if v == 14:
                if manual:
                    f["duration"] = f.get("manual_duration")
                else:
                    f["manual_duration"] = None
            if f.get("name") is None and any(f.get(x) is None or f.get(x) == B.EPOCH
                                             for x in (("start", "finish") if v == 12 else
                                                       ("start", "finish", "created"))):
                p.remove_task(t)
                if v == 9:
                    self.null_task(get_int(data, 0), get_int(data, 4))
                elif get_int(data, 4) not in self.null_order:
                    self.null_task(get_int(data, 0), get_int(data, 4))
                continue
            if v == 9:
                self.order[t.id] = t.uid
            elif data2 is None or len(data2) < 24:
                self.null_order[t.id] = t.uid
            else:
                self.order[get_long(data2, 16)] = t.uid
        p.auto_wbs = auto_wbs
        current = None
        for t in sorted(external, key=lambda x: x.id or 0):
            sp = t.f.get("subproject_file")
            if sp is None:
                if current is not None:
                    t.f["subproject_file"] = current
            else:
                current = sp

    def relations(self):
        d = "TBkndCons/"
        if not any(self.cf.exists(self.dir + d + s) for s in ("FixedMeta", "FixedData")):
            return
        p = self.p
        meta = FixedMeta(self.stream(d + "FixedMeta"), 10)
        fd = FixedData.by_meta_size(meta, 20, self.coded(d + "FixedData"))
        late = self.v == 14 and self.app > APP_2010
        units_at, amount_at = (18, 14) if late else (14, 16)
        for k in range(meta.count):
            m = meta.item(k)
            if get_short(m, 0) != 0:
                continue
            idx = fd.index_of(get_int(m, 4))
            if idx == -1:
                continue
            data = fd.item(idx)
            if data is None:
                raise IndexError(idx)
            if len(data) < 14:
                continue
            a, b = get_int(data, 4), get_int(data, 8)
            if a == 0 or b == 0 or a == b:
                continue
            pred, succ = p.uids.get(a), p.uids.get(b)
            if pred is None or succ is None:
                continue
            kind = get_short(data, 12)
            lag = B.adjusted(p, get_int(data, amount_at), B.duration_units(get_short(data, units_at)))
            p.add_predecessor(succ, pred, RELATION_TYPES[kind] if kind < 4 else "FINISH_START", lag)

    def assignments(self):
        v, p = self.v, self.p
        fm = FieldMap(v, "assignment", self.pp, p)
        d = "TBkndAssn/"
        vm = VarMeta(self.stream(d + "VarMeta"), v != 9)
        vd = Var2Data(vm, self.stream(d + "Var2Data"))
        meta = FixedMeta(self.stream(d + "FixedMeta"), 34)
        fd2 = None
        if v == 14:
            fd, count = FixedData.blocks(110, self.coded(d + "FixedData")), meta.count
        elif v == 12:
            fd, count = FixedData.by_meta(meta, self.coded(d + "FixedData")), len(meta.items)
        else:
            fd, count = FixedData.blocks(142, self.coded(d + "FixedData")), len(meta.items)
            if len(fd.items) != len(meta.items):
                fd = FixedData.by_meta(meta, self.coded(d + "FixedData"))
        if v != 9:
            fd2 = FixedData.blocks(48, self.coded(d + "Fixed2Data"))
        mx = fm.max[0]
        for k in range(count):
            m = meta.item(k)
            if m is None or m[0] != 0:
                continue
            idx = fd.index_of(get_int(m, 4))
            data = fd.item(idx)
            if data is None:
                continue
            if len(data) < mx:
                data = data + bytes(mx - len(data))
            aid = get_int(data, fm.offset("unique_id"))
            if aid not in vm.table:
                continue
            t = p.uids.get(get_int(data, fm.offset("task_unique_id")))
            if t is None:
                continue
            r = p.resource_uids.get(get_int(data, fm.offset("resource_unique_id")))
            have = p.assigned.get(t.uid, [])
            if any((r is None and p.resource_uids.get(a.resource_uid) is None) or
                   (r is not None and r.uid == a.resource_uid) for a in have):
                continue
            a = Assignment(None if r is None else r.uid)
            if t.uid is not None:
                p.assigned.setdefault(t.uid, []).append(a)
            values = fm.read_all(aid, [data, fd2.item(idx) if fd2 else None], vd)
            if "units" in values:
                a.units = values["units"]
            if "resource_unique_id" in values:
                a.resource_uid = values["resource_unique_id"]
            if a.resource_uid == NULL_RESOURCE:
                a.resource_uid = None


def properties(p: Project, pp: dict, cf):
    """ProjectPropertiesReader: the dates, duration units and day lengths from the project Props `pp`, and the
    title from SummaryInformation, else from the Props."""

    def ts(key):
        item = pp.get(key)
        if item is None:
            return None
        get_int(item, 0)
        return B.timestamp(item, 0)

    p.start, p.finish, p.status = ts(K_START), ts(K_FINISH), ts(K_STATUS)
    p.duration_units = B.duration_units(get_short(pp[K_UNITS], 0) if K_UNITS in pp else 0)
    p.minutes_per_day = get_int(pp[K_MIN_DAY], 0) if K_MIN_DAY in pp else 0
    p.minutes_per_week = get_int(pp[K_MIN_WEEK], 0) if K_MIN_WEEK in pp else 0
    p.days_per_month = get_short(pp[K_DAYS_MONTH], 0) if K_DAYS_MONTH in pp else 0
    title = summary_title(cf)
    if not title:
        title = B.unicode(pp[K_TITLE], 0) if K_TITLE in pp else None
    p.title = title or None


def post_process(p: Project, order: dict, null_order: dict):
    """postProcessTasks: the IDs again, in the order the task data gives, with the null tasks put back in."""
    step = (len(null_order) // 1000 + 1) * 2000
    nxt = 0 if p.uids.get(0) is not None else step
    slots = {}
    for key in sorted(order):
        slots[nxt] = order[key]
        nxt += step
    inserted, used = 0, {}
    for tid in sorted(null_order):
        base = (tid - inserted) * step
        target, prev = base, used.get(base)
        offset = 0 if prev is None else prev + 1
        inserted += 1
        while target in slots:
            offset += 1
            if offset == step:
                raise Unfixable()
            target = base - (step - offset)
        used[base] = offset
        slots[target] = null_order[tid]
    nxt = 0 if p.uids.get(0) is not None else 1
    for key in sorted(slots):
        t = p.uids.get(slots[key])
        if t is not None:
            p.set_id(t, nxt)
        nxt += 1


def finish(p: Project):
    """MPPReader.read after the variant reader: updateStructure with outline numbers, then each task's summary flag,
    the relations a task's own summary has to it removed and an empty baseline field set from its estimated one
    (copyEstimatedBaselineFields)."""
    update_structure(p)
    for t in p.tasks:
        external_project = t.f.get("subproject_file") is not None and not t.f.get("external_task")
        t.summary = bool(t.children) or external_project
        for name in ("duration", "start", "finish"):
            if t.f.get("baseline_" + name) is None:
                t.f["baseline_" + name] = t.f.get("baseline_estimated_" + name)
        if not t.null and (t.f.get("baseline_start") is not None or t.f.get("baseline_finish") is not None):
            p.baseline = True
    for t in p.tasks:
        for rel in [r for r in p.predecessors(t) if t.outline_number is not None
                    and r.pred.outline_number is not None
                    and t.outline_number.startswith(r.pred.outline_number + ".")]:
            p.remove_predecessor(t, rel)
    if p.start is None:
        p.start = min((d for t in p.tasks if (t.uid or 0) != 0 and (d := _task_start(t)) is not None), default=None)
    if p.finish is None:
        p.finish = max((d for t in p.tasks if (t.uid or 0) != 0
                        and (d := t.f.get("actual_finish") or t.f.get("finish")) is not None), default=None)


def _task_start(t: Task):
    if t.milestone:
        return t.f.get("actual_finish") or t.f.get("finish")
    return t.f.get("actual_start") or t.f.get("start")


def update_structure(p: Project):
    """TaskContainer.updateStructure: the tasks sorted by ID, each under the last task one outline level up, with a
    WBS (when the file gives none, or gives none at all) and an outline number."""
    if len(p.tasks) <= 1:
        return
    p.tasks.sort(key=lambda t: t.id or 0)
    root, last, last_level = [], None, -1
    for t in p.tasks:
        t.children = []
        parent = None
        if not t.null:
            level = t.f.get("outline_level") or 0
            if last is not None:
                if level == last_level:
                    parent, level = last.parent, last_level
                elif level > last_level:
                    parent = last
                else:
                    while level <= last_level:
                        parent = last.parent
                        if parent is None:
                            break
                        last_level, last = parent.f.get("outline_level") or 0, parent
            last, last_level = t, level
            siblings = len(root if parent is None else parent.children) + 1
            if p.auto_wbs or t.f.get("wbs") is None:
                t.f["wbs"] = _number(parent.f.get("wbs") if parent else None, siblings, t, False)
            t.outline_number = _number(parent.outline_number if parent else None, siblings, t, True)
        if parent is None:
            root.append(t)
        else:
            t.parent = parent
            parent.children.append(t)


def _number(up, siblings: int, t: Task, outline: bool) -> str:
    """Task.generateWBS and generateOutlineNumber: "0" for unique ID 0 at the top, else the place among the
    siblings, after the parent's number unless that is "0" (an outline number first loses its last ".0" part)."""
    if up is None:
        return "0" if (t.uid or 0) == 0 else str(siblings)
    if outline and ".0" in up:
        up = up[:up.rindex(".0")]
    return str(siblings) if up == "0" else f"{up}.{siblings}"


def max_units(r: Resource, now: datetime):
    """Resource.getMaxUnits, always calculated: the units of the availability entry `now` falls in (AvailabilityFactory
    builds the table from the availability data, else one entry from the fixed data's dates and max units)."""
    raw, table = r.f.get("availability"), []
    if raw is None:
        table.append((r.f.get("available_from") or START_NA, r.f.get("available_to") or END_NA, r.f.get("max_units")))
    else:
        off = 12
        for _ in range(get_short(raw, 0)):
            value = get_double(raw, off + 4)
            if value != 0:
                start = B.EPOCH + timedelta(seconds=get_int(raw, off) * 6)
                end = B.EPOCH + timedelta(seconds=get_int(raw, off + 20) * 6) - timedelta(minutes=1)
                table.append((max(start, START_NA), min(end, END_NA), value / 100))
            off += 20
        table.sort(key=lambda e: (e[0], e[1]))
    for start, end, units in table:
        if now < start:
            return None
        if now <= end:
            return units
    return None


def summary_title(cf) -> str:
    """SummaryInformation's title ("" without the stream); ValueError when the stream is no property set."""
    if not cf.exists(doc_fib.SUMMARY_STREAM):
        return ""
    sets = doc_fib.property_sets(cf.read(doc_fib.SUMMARY_STREAM))
    title = sets.get(doc_fib.SUMMARY_FMTID, ({}, {}))[0].get(2)
    return title if isinstance(title, str) else ""
