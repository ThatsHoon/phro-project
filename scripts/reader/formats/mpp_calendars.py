# Ported from MPXJ (https://www.mpxj.org), copyright (c) Packwood Software, under the GNU Lesser General Public
# License 2.1 or later. This module is distributed under the same license: see https://www.gnu.org/licenses/lgpl-2.1.
"""The calendars of an MPP9, MPP12 or MPP14 project, as MPXJ reads them. A port of org.mpxj.mpp
AbstractCalendarFactory, MPP9CalendarFactory, MPP12CalendarFactory, MPP14CalendarFactory and
AbstractCalendarAndExceptionFactory, and of the parts of ProjectCalendar, ProjectCalendarDays,
ProjectCalendarContainer and RecurringData they rely on; `mpp8` reads MPP8's with the helpers here, and
`mpp_read.finish` prunes and names the resource calendars as MPPReader does.

Days are numbered from Sunday (0) to Saturday (6); a time is seconds after midnight, and a range's end wraps past
midnight as LocalTime.plus does. A day's hours are None when the calendar has none of its own for it.
"""
from datetime import date, timedelta

from . import mpp_blocks as B
from .mpp_blocks import FixedData, FixedMeta, Var2Data, VarMeta
from .mpp_read import APP_2010, _unicode, get_byte, get_int, get_short

EPOCH = date(1983, 12, 31)
K_DEFAULT_HOURS, K_DEFAULT_NAME = 37753736, 37748750
DEFAULT_NAME = "Standard"
DEFAULT_WEEK = (False, True, True, True, True, True, False)
DEFAULT_HOURS = ((8 * 3600, 12 * 3600), (13 * 3600, 17 * 3600))
RECURRENCE = (None, "DAILY", "YEARLY", "YEARLY", "MONTHLY", "MONTHLY", "WEEKLY", "DAILY")
RELATIVE = (False, False, False, True, False, True)
WORKING, NON_WORKING, DEFAULT = "WORKING", "NON_WORKING", "DEFAULT"


class Days:
    """ProjectCalendarDays: a name and each day's type and hours; setting a type drops a default day's hours and
    empties a non-working day's (setCalendarDayType)."""

    def __init__(self):
        self.name = None
        self.types, self.hours = [None] * 7, [None] * 7

    def add_hours(self, day: int) -> list:
        self.hours[day] = []
        return self.hours[day]

    def set_type(self, day: int, kind: str):
        self.types[day] = kind
        if kind == DEFAULT:
            self.hours[day] = None
        elif kind == NON_WORKING:
            if self.hours[day] is None:
                self.hours[day] = []
            else:
                self.hours[day].clear()

    def set_working(self, day: int, working: bool):
        self.set_type(day, WORKING if working else NON_WORKING)


class Week(Days):
    """ProjectCalendarWeek: a work week's days and its date range (start, finish)."""

    def __init__(self):
        super().__init__()
        self.start = self.finish = None


class Calendar(Days):
    """ProjectCalendar: unique ID, parent, days, exceptions and work weeks."""

    def __init__(self):
        super().__init__()
        self.uid = self.parent = None
        self.exceptions, self.weeks = [], []

    def set_parent(self, parent):
        """setParent: a day with no type becomes a default one."""
        if parent is not self:
            self.parent = parent
            for day in range(7):
                if self.types[day] is None:
                    self.set_type(day, DEFAULT)

    def day_type(self, day: int):
        t = self.types[day]
        if t == DEFAULT:
            if self.parent is None:
                return NON_WORKING if day in (0, 6) else WORKING
            return self.parent.day_type(day)
        return t

    def working(self, day: int) -> bool:
        return self.day_type(day) == WORKING

    def get_hours(self, day: int):
        h = self.hours[day]
        return self.parent.get_hours(day) if h is None and self.parent is not None else h


class CalendarException:
    """ProjectCalendarException: from and to dates, a name, the working ranges ([] for a non-working one) and the
    recurrence (None for a plain exception)."""

    def __init__(self, start, end, recurring=None):
        self.start, self.end, self.recurring = start, end, recurring
        self.name, self.ranges = None, []


class Recurring:
    """RecurringData: start and finish dates, type, relative flag, occurrences, frequency, day number, the days
    (weekly, or the one day of the week of a relative month or year) and the month."""

    def __init__(self, start, finish, kind, relative, occurrences):
        self.start, self.finish, self.kind, self.relative = start, finish, kind, relative
        self.occurrences, self.frequency = occurrences, None
        self.day_number = self.month = None
        self.days = set()


def default_base() -> Calendar:
    """ProjectCalendarContainer.addDefaultBaseCalendar."""
    cal = Calendar()
    cal.name = DEFAULT_NAME
    for day in range(7):
        cal.set_working(day, day not in (0, 6))
    for day in range(7):
        hours = cal.add_hours(day)
        if day not in (0, 6):
            hours.extend(DEFAULT_HOURS)
    return cal


def default_derived() -> Calendar:
    cal = Calendar()
    for day in range(7):
        cal.set_type(day, DEFAULT)
    return cal


def get_date(b, o):
    d = get_short(b, o)
    return None if d == 65535 else EPOCH + timedelta(days=d)


def get_time(b, o) -> int:
    return get_short(b, o) // 10 * 60 % 86400


def time_range(b, start_at: int, duration_at: int) -> tuple:
    """A range from MPPUtility.getTime and getDuration (a u16 in tenths of a minute)."""
    start = get_time(b, start_at)
    return start, (start + get_short(b, duration_at) * 6) % 86400


def _day_of_week(value: int) -> int:
    """DayOfWeekHelper.getInstance, Sunday 1; a value out of range makes RecurringData.setDayOfWeek fail."""
    if not 1 <= value <= 7:
        raise ValueError("recurrence day of week")
    return value - 1


def by_name(cals: list, name):
    """ProjectCalendarContainer.getByName: the first calendar whose name equals `name` ignoring case."""
    if name:
        for c in cals:
            if c.name is not None and c.name.lower() == name.lower():
                return c
    return None


def default_of(cals: list, props: dict) -> Calendar:
    """The project's default calendar: the one the project properties name, else findOrCreateDefaultCalendar
    ("Standard", the first calendar, or a new default base calendar)."""
    raw = props.get(K_DEFAULT_NAME)
    found = by_name(cals, None if raw is None else B.unicode(raw, 0)) or by_name(cals, DEFAULT_NAME)
    if found is None:
        if cals:
            return cals[0]
        found = default_base()
        found.uid = 1
        cals.append(found)
    return found


def _hours(data, default, cal: Calendar, base: bool, at: int):
    """processCalendarHours: each day's 60 bytes from `at`, a flag of 1 taking the default calendar's day (or the
    default week when there is none) on a base calendar and the parent's on a derived one."""
    for day in range(7):
        off = at + 60 * day
        flag = 1 if data is None else get_short(data, off)
        if flag == 1:
            if not base:
                cal.set_type(day, DEFAULT)
                continue
            hours = cal.add_hours(day)
            if default is None:
                cal.set_working(day, DEFAULT_WEEK[day])
                if cal.working(day):
                    hours.extend(DEFAULT_HOURS)
            else:
                working = default.working(day)
                cal.set_working(day, working)
                if working:
                    hours.extend(default.get_hours(day))
            continue
        ranges = [time_range(data, off + 8 + 2 * k, off + 20 + 4 * k) for k in range(get_short(data, off + 2))]
        if ranges:
            cal.set_working(day, True)
            cal.add_hours(day).extend(ranges)
        else:
            if base:
                cal.add_hours(day)
            cal.set_working(day, False)


def _exceptions9(data: bytes, cal: Calendar):
    for k in range(get_short(data, 0)):
        off = 4 + 60 * 7 + 64 * k
        e = CalendarException(get_date(data, off), get_date(data, off + 2))
        cal.exceptions.append(e)
        e.ranges = [time_range(data, off + 12 + 2 * i, off + 24 + 4 * i) for i in range(get_short(data, off + 6))]


def _recurring(data: bytes, off: int, start, end):
    """readRecurringData: None for a daily recurrence every day (a plain exception); ValueError for a type MPXJ's
    switch cannot take."""
    value = get_short(data, off + 72)
    kind = RECURRENCE[value] if value < len(RECURRENCE) else None
    r = Recurring(start, end, kind, value < len(RELATIVE) and RELATIVE[value], get_short(data, off + 4))
    if kind is None:
        raise ValueError("recurrence type")
    if kind == "DAILY":
        r.frequency = 1 if value == 1 else get_short(data, off + 76)
    elif kind == "WEEKLY":
        bits = get_byte(data, off + 76)
        r.days = {d for d in range(7) if bits & 1 << d}
        r.frequency = get_short(data, off + 78)
    elif kind == "MONTHLY":
        if r.relative:
            r.days = {_day_of_week(get_byte(data, off + 77) - 2)}
            r.day_number = get_byte(data, off + 76) + 1
            r.frequency = get_short(data, off + 78)
        else:
            r.day_number = get_byte(data, off + 76)
            r.frequency = get_byte(data, off + 78)
    else:
        if r.relative:
            r.days = {_day_of_week(get_byte(data, off + 78) - 2)}
            r.day_number = get_byte(data, off + 77) + 1
        else:
            r.day_number = get_byte(data, off + 77)
        r.month = get_byte(data, off + 76) + 1
    return None if kind == "DAILY" and r.frequency == 1 else r


def _rounded(n: int) -> int:
    return (n // 4 + 1) * 4 if n % 4 else n


def _exceptions12(data: bytes, cal: Calendar):
    """AbstractCalendarAndExceptionFactory.processCalendarExceptions: the exceptions after the 420 bytes of hours,
    each 92 bytes and a name, then the work weeks."""
    if len(data) <= 420:
        return
    count, off = get_short(data, 420), 424
    for _ in range(count):
        if off + 92 > len(data):
            break
        start, end = get_date(data, off), get_date(data, off + 2)
        e = CalendarException(start, end, _recurring(data, off, start, end))
        cal.exceptions.append(e)
        e.ranges = [time_range(data, off + 20 + 2 * i, off + 32 + 4 * i) for i in range(get_short(data, off + 14))]
        n = get_int(data, off + 88)
        if n < 0:
            return
        n = _rounded(n)
        if n:
            e.name = _unicode(data, off + 92)
        off += 92 + n
    _weeks(data, off, cal)


def _weeks(data: bytes, off: int, cal: Calendar):
    """processWorkWeeks: 7 days of 60 bytes, the start and finish dates, 8 bytes and a name, while they fit."""
    off += 4
    while len(data) >= off + 7 * 60 + 16:
        w = Week()
        cal.weeks.append(w)
        for day in range(7):
            _week_day(data, off, w, day)
            off += 60
        start, finish = get_date(data, off), get_date(data, off + 2)
        off += 12
        n = get_int(data, off)
        if n < 0:
            return
        n = _rounded(n)
        off += 4
        if n:
            w.name = _unicode(data, off, n)
            off += n
        w.start, w.finish = start, finish


def _week_day(data: bytes, off: int, w: Week, day: int):
    if get_short(data, off) == 1:
        w.set_type(day, DEFAULT)
        return
    hours = w.add_hours(day)
    count = get_short(data, off + 2)
    if count == 0:
        w.set_type(day, NON_WORKING)
        return
    w.set_type(day, WORKING)
    for k in range(count):
        tenths = get_int(data, off + 20 + 4 * k)
        if tenths < 1 or tenths > 14400:
            continue
        start = get_time(data, off + 8 + 2 * k)
        hours.append((start, (start + tenths * 6) % 86400))
    if not hours:
        w.set_type(day, NON_WORKING)


def read(reader) -> tuple:
    """processCalendarData of an mpp_read._Reader: (the calendars in order, the default calendar, {resource unique
    ID: calendar}). A derived calendar whose base is missing or has no name is left out."""
    v, pp = reader.v, reader.pp
    d = "TBkndCal/"
    vm = VarMeta(reader.stream(d + "VarMeta"), v != 9)
    vd = Var2Data(vm, reader.stream(d + "Var2Data"))
    meta = FixedMeta(reader.stream(d + "FixedMeta"), 10)
    fd = FixedData.by_meta(meta, reader.coded(d + "FixedData"), 12)
    if reader.cf.exists(reader.dir + d + "Fixed2Meta"):
        FixedData.by_meta(FixedMeta(reader.stream(d + "Fixed2Meta"), 9), reader.coded(d + "Fixed2Data"), 48)
    at = 4 if v == 9 else 0
    cid, bid, rid = (8, 0, 4) if v == 14 and reader.app > APP_2010 else (0, 4, 8)
    data_type = 3 if v == 9 else 8
    default_data = pp.get(K_DEFAULT_HOURS)
    default = Calendar()
    _hours(default_data, None, default, True, at)
    cals, by_id, pending, resources = [], {}, [], {}
    for k in range(len(meta.items)):
        item = fd.item(k)
        if item is None or len(item) < 8:
            continue
        off = 0
        while off + 12 <= len(item):
            uid, base = get_int(item, off + cid), get_int(item, off + bid)
            if uid > 0 and uid not in by_id:
                data = vd.get(uid, data_type)
                if base <= 0 or base == uid:
                    if data is not None or default_data is not None:
                        cal = Calendar()
                        data = default_data if data is None else data
                    else:
                        cal = default_base()
                    cal.name = vd.text(uid, 1)
                    res = get_int(item, off + rid)
                    if res > 0 and res not in resources:
                        resources[res] = cal
                else:
                    cal = Calendar() if data is not None else default_derived()
                    pending.append((cal, base))
                    resources[get_int(item, off + rid)] = cal
                cals.append(cal)
                cal.uid = uid
                if data is None:
                    if base <= 0:
                        for day in range(7):
                            cal.add_hours(day)
                else:
                    _hours(data, default, cal, base <= 0, at)
                    (_exceptions9 if v == 9 else _exceptions12)(data, cal)
                by_id[uid] = cal
            off += 12
    for cal, base in pending:
        parent = by_id.get(base)
        if parent is not None and parent.name is not None:
            cal.set_parent(parent)
        else:
            cals.remove(cal)
    return cals, default_of(cals, pp), resources
