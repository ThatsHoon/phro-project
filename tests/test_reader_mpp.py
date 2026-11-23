"""Microsoft Project (mpp): routing by the CompObj, the pure cell helpers, the Doc built from an mpp_read.Project,
the note text (mpp_notes) against what MPXJ's RtfHelper.strip gave, and the reads of MPXJ's own test files against
MPXJ's output (skipped, saying why, when they are not on this machine).

Run: python -I -X utf8 tests/test_reader_mpp.py
"""
import hashlib
import json
import math
import re
import struct
import sys
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

from reader import limits, markdown  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import cfb_route, mpp, mpp_calendars, mpp_notes, mpp_read, sheet_grid  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Bold, Context, Doc, Heading, ListItem, Para, Table, Text  # noqa: E402
from reader_fix import cfb_bytes, comp_obj  # noqa: E402

MSPROJECT = comp_obj(b"MSProject.MPP9", b"Microsoft Project")
HEADER = "\nID|Outline|Name|Duration|Start|Finish|% Complete|Predecessors|Resources\n"
BENCH = Path("C:/Users/hunje/research/ai-document-reader-plugins/bench")
JUNIT = BENCH / "engines" / "mpxj" / "src" / "junit" / "data"
JUNIT_REF = Path(__file__).parent / "fixtures" / "mpp" / "junit.json"
BASELINES = Path(__file__).parent / "fixtures" / "mpp" / "baselines.json"
CALENDARS = Path(__file__).parent / "fixtures" / "mpp" / "calendars.json"
CALENDAR_LIST = re.compile(r"\n\n## Calendars\n.*?(?=\n\n## |\n\Z)", re.S)


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def project_file(d, streams=None, name="plan.mpp", data=None):
    """A compound file (a CompObj naming MS Project unless `streams` says otherwise, or the bytes `data`) at d/name."""
    p = Path(d) / name
    p.write_bytes(data if data is not None else cfb_bytes({"\x01CompObj": MSPROJECT} if streams is None
                                                          else streams)[0])
    return p


def parse_bytes(data, name="plan.mpp"):
    with tempfile.TemporaryDirectory() as d:
        return mpp.parse(project_file(d, data=data, name=name), Context())


def render(doc, ctx, hidden=False):
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped)


def test_not_msproject_routes():
    for streams in ({"Other": b"x"}, {"\x01CompObj": comp_obj(b"Word.Document.8")}, {"\x01CompObj": b"\x01" * 9}):
        e = raises(ReadError, parse_bytes, cfb_bytes(streams)[0])
        assert e.kind == "damaged" and e.detail == "no MSProject CompObj", (streams, e)
    e = raises(ReadError, parse_bytes, cfb_bytes({"EncryptedPackage": b"x"})[0])
    assert e.kind == "encrypted"
    e = raises(ReadError, parse_bytes, b"not a compound file at all")
    assert e.kind == "damaged" and e.detail == "not a Microsoft Project file"
    e = raises(ReadError, parse_bytes, cfb_bytes({"\x01CompObj": MSPROJECT})[0])
    assert e.kind == "damaged" and e.detail == mpp_read.DAMAGED, e
    e = raises(ReadError, parse_bytes, cfb_bytes({"\x01CompObj": MSPROJECT, "WordDocument": bytes(64)})[0])
    assert e.detail == mpp_read.DAMAGED, e
    e = raises(ReadError, parse_bytes, cfb_bytes({"\x01CompObj": comp_obj(b"MSProject.MPP4", b"x")})[0])
    assert e.kind == "damaged", e


def test_not_msproject_handed_on():
    seen = []
    real = cfb_route.parse

    def fake(fmt, path, ctx):
        seen.append(fmt)
        return Doc(title=fmt)

    cfb_route.parse = fake
    try:
        assert parse_bytes(cfb_bytes({"VisioDocument": b"Visio (TM) Drawing\r\n"})[0]).title == "vsd"
        doc = parse_bytes(cfb_bytes({"WordDocument": bytes(64), "\x01CompObj": comp_obj(b"Word.Document.8")})[0])
        assert doc.title == "doc" and seen == ["vsd", "doc"], seen
    finally:
        cfb_route.parse = real


def test_zip_handed_to_zip_route():
    seen = []
    real = mpp.zip_route.parse

    def fake(path, ctx):
        seen.append(Path(path).name)
        return Doc(title="zip")

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "z.mpp"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
        e = raises(ReadError, mpp.parse, p, Context())
        assert e.detail != "not a Microsoft Project file", e
        mpp.zip_route.parse = fake
        try:
            assert mpp.parse(p, Context()).title == "zip"
        finally:
            mpp.zip_route.parse = real
        assert seen == ["z.mpp"]


def test_broken_fat_damaged():
    for streams in ({"\x01CompObj": MSPROJECT}, {"\x01CompObj": MSPROJECT, "Big": bytes(9000)}):
        data, info = cfb_bytes(streams)
        b = bytearray(data)
        for sid in info["fat"]:
            b[(sid + 1) * 512:(sid + 2) * 512] = struct.pack("<I", 5) * 128
        e = raises(ReadError, parse_bytes, bytes(b))
        assert e.kind == "damaged", e
        b = bytearray(data)
        first_mini = struct.unpack_from("<I", b, 0x3C)[0]
        for i in range(128):
            struct.pack_into("<I", b, (first_mini + 1) * 512 + 4 * i, i)
        e = raises(ReadError, parse_bytes, bytes(b))
        assert e.kind == "damaged" and e.detail == "no MSProject CompObj", e


def test_junit_parity():
    """Each of MPXJ's own test files (the bench corpus's mpp files among them) reads to the Markdown MPXJ's MPPReader
    gave for it (its sha256, first 16 hex digits, in fixtures/mpp/junit.json), or is refused as MPXJ refused it. The
    39 files with a baseline date carry the Baseline columns MPXJ's Markdown had none of: their other cells are
    MPXJ's, and their baselines are MPXJ's by test_baselines_match_mpxj. The Calendars list, which MPXJ's Markdown
    had no part of, is left out of the hash: test_calendars_match_mpxj checks the calendars."""
    if not JUNIT.is_dir():
        print(f"skip: no MPXJ test data at {JUNIT}")
        return
    want = json.loads(JUNIT_REF.read_text(encoding="utf-8"))
    bad = []
    for rel, ref in want.items():
        ctx = Context()
        try:
            md = CALENDAR_LIST.sub("", render(mpp.parse(JUNIT / rel, ctx), ctx))
            got = hashlib.sha256(md.encode("utf-8")).hexdigest()[:16]
        except ReadError as e:
            got = e.kind
        if got != ref:
            bad.append(rel)
    assert not bad, (len(bad), bad[:5])


def test_baselines_match_mpxj():
    """Every task of MPXJ's test files has the baseline start, finish and duration MPXJ 16.10.0 gave it (the tasks
    with a baseline date or a duration other than 0, in fixtures/mpp/baselines.json); no other task has a date."""
    if not JUNIT.is_dir():
        print(f"skip: no MPXJ test data at {JUNIT}")
        return
    want = json.loads(BASELINES.read_text(encoding="utf-8"))
    bad = []
    for rel in json.loads(JUNIT_REF.read_text(encoding="utf-8")):
        try:
            with Compound(JUNIT / rel) as cf:
                p = mpp_read.read(cf)
        except ReadError:
            continue
        rows = want.get(rel, {})
        for t in p.tasks:
            if t.null:
                continue
            f, d = t.f, t.f.get("baseline_duration")
            got = [None if f.get(k) is None else f[k].isoformat(timespec="minutes")
                   for k in ("baseline_start", "baseline_finish")]
            got.append(None if not isinstance(d, tuple) else [float(d[0]), d[1]])
            ref = rows.get(str(t.uid))
            if ref is None:
                ok = got[0] is None and got[1] is None and (got[2] is None or got[2][0] == 0)
            else:
                ok = got[:2] == ref[:2] and (got[2] is None) == (ref[2] is None) and (
                    got[2] is None or math.isclose(got[2][0], ref[2][0], abs_tol=1e-9) and got[2][1] == ref[2][1])
            if not ok:
                bad.append((rel, t.uid, got, ref))
        if p.baseline != any(r[0] or r[1] for r in rows.values()):
            bad.append((rel, "baseline", p.baseline))
    assert not bad, (len(bad), bad[:5])


def _clock(seconds):
    h, m, s = seconds // 3600, seconds // 60 % 60, seconds % 60
    return f"{h:02}:{m:02}" + (f":{s:02}" if s else "")


def _ranges(hours):
    return None if hours is None else [[_clock(a), _clock(b)] for a, b in hours]


def _iso(d):
    return None if d is None else d.isoformat()


def _days(c):
    return [[c.types[i], _ranges(c.hours[i])] for i in range(7)]


def _recurrence(r):
    if r is None:
        return None
    names = ("SUNDAY", "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY")
    first = next((names[i] for i in (1, 2, 3, 4, 5, 6, 0) if i in r.days), None)
    return [_iso(r.start), _iso(r.finish), r.kind.capitalize(), bool(r.relative), r.occurrences, r.frequency,
            r.day_number, first, r.month, None]


def calendar_dump(p) -> dict:
    """The calendars as the MPXJ dump behind fixtures/mpp/calendars.json lists them: the default's unique ID, and
    each calendar's unique ID, name, parent, days (Sunday first: own type, own ranges), exceptions (sorted), work
    weeks and the resources using it."""
    cals = []
    for c in p.calendars:
        exc = sorted(([None if e.recurring else _iso(e.start), None if e.recurring else _iso(e.end), e.name,
                       _ranges(e.ranges), _recurrence(e.recurring)] for e in c.exceptions), key=json.dumps)
        cals.append({"uid": c.uid, "name": c.name, "parent": None if c.parent is None else c.parent.uid,
                     "days": _days(c), "exceptions": exc,
                     "weeks": [[w.name, _iso(w.start), _iso(w.finish), _days(w)] for w in c.weeks],
                     "resources": sorted(r.uid for r in p.resources if r.calendar is c)})
    return {"default": None if p.default_calendar is None else p.default_calendar.uid, "calendars": cals}


def test_calendars_match_mpxj():
    """Every calendar of MPXJ's test files, with its days, hours, exceptions, recurrences, work weeks, parent and
    resources, is the one MPXJ 16.10.0 read (the sha256 of that dump, first 16 hex digits, in
    fixtures/mpp/calendars.json); the default calendar is MPXJ's too."""
    if not JUNIT.is_dir():
        print(f"skip: no MPXJ test data at {JUNIT}")
        return
    want = json.loads(CALENDARS.read_text(encoding="utf-8"))
    bad = []
    for rel, ref in want.items():
        with Compound(JUNIT / rel) as cf:
            p = mpp_read.read(cf)
        dump = json.dumps(calendar_dump(p), sort_keys=True, separators=(",", ":"))
        if hashlib.sha256(dump.encode("utf-8")).hexdigest()[:16] != ref:
            bad.append(rel)
    assert not bad, (len(bad), bad[:5])


def _day_bytes(ranges=None, default=False, tenths=False) -> bytes:
    """A 60-byte day: the flag (1 for default), the range count, starts at 8 and durations at 20 (u16 tenths of a
    minute, or i32 with `tenths` as a work week's are)."""
    b = bytearray(60)
    if default:
        b[0] = 1
        return bytes(b)
    ranges = ranges or []
    struct.pack_into("<H", b, 2, len(ranges))
    for k, (start, minutes) in enumerate(ranges):
        struct.pack_into("<H", b, 8 + 2 * k, start * 10)
        struct.pack_into("<i" if tenths else "<H", b, 20 + 4 * k, minutes * 10)
    return bytes(b)


def _days_since(d) -> int:
    return (d - date(1983, 12, 31)).days


def calendar_data() -> bytes:
    """MPP12/14 calendar data: Monday 09:00-17:00 and the rest default; an exception recurring every 2 weeks on
    Monday and Friday, named Off; then a work week Q1 with a default Sunday, Monday 10:00-14:00 (and a range of 0
    minutes, left out) and a non-working Tuesday."""
    hours = b"".join(_day_bytes(default=True) if day != 1 else _day_bytes([(9 * 60, 8 * 60)]) for day in range(7))
    exc = bytearray(92)
    struct.pack_into("<HHH", exc, 0, _days_since(date(2026, 1, 2)), _days_since(date(2026, 6, 26)), 13)
    struct.pack_into("<H", exc, 72, 6)
    exc[76] = 0x22
    struct.pack_into("<H", exc, 78, 2)
    struct.pack_into("<i", exc, 88, 6)
    week = [_day_bytes(default=True), _day_bytes([(10 * 60, 4 * 60), (0, 0)], tenths=True), _day_bytes()]
    week = b"".join(week + [_day_bytes(default=True)] * 4)
    week += struct.pack("<HH8xi", _days_since(date(2026, 2, 2)), _days_since(date(2026, 3, 1)), 4)
    return (hours + struct.pack("<H2x", 1) + bytes(exc) + "Off".encode("utf-16-le") + b"\0\0" + bytes(4) + week
            + "Q1".encode("utf-16-le"))


def test_calendar_exceptions_and_work_weeks_read():
    """The hours, a recurring exception and a work week (none is in MPXJ's test files) of MPP12/14 calendar data;
    a recurrence type MPXJ cannot take fails."""
    data = calendar_data()
    default = mpp_calendars.Calendar()
    mpp_calendars._hours(None, None, default, True, 0)
    cal = mpp_calendars.Calendar()
    mpp_calendars._hours(data, default, cal, True, 0)
    mpp_calendars._exceptions12(data, cal)
    assert cal.types[1] == "WORKING" and cal.hours[1] == [(9 * 3600, 17 * 3600)]
    assert cal.types[0] == "NON_WORKING" and cal.hours[0] == [] and cal.hours[2] == list(mpp_calendars.DEFAULT_HOURS)
    (e,) = cal.exceptions
    r = e.recurring
    assert e.name == "Off" and e.ranges == [] and r.kind == "WEEKLY" and r.days == {1, 5} and r.frequency == 2
    assert (r.start, r.finish, r.occurrences) == (date(2026, 1, 2), date(2026, 6, 26), 13)
    (w,) = cal.weeks
    assert w.name == "Q1" and (w.start, w.finish) == (date(2026, 2, 2), date(2026, 3, 1))
    assert w.types[:3] == ["DEFAULT", "WORKING", "NON_WORKING"] and w.hours[0] is None and w.hours[2] == []
    assert w.hours[1] == [(10 * 3600, 14 * 3600)], w.hours[1]
    bad = bytearray(data)
    struct.pack_into("<H", bad, 424 + 72, 0)
    raises(ValueError, mpp_calendars._exceptions12, bytes(bad), mpp_calendars.Calendar())


def calendar_project():
    """Standard (the default) with a night shift Monday; Ann's calendar based on it with Friday off, a holiday and a
    monthly working day; Bob's sets nothing of its own."""
    std = mpp_calendars.default_base()
    std.uid = 1
    std.hours[1] = [(22 * 3600, 0)]
    ann = mpp_calendars.default_derived()
    ann.uid, ann.name = 2, "Ann"
    ann.set_parent(std)
    ann.set_working(5, False)
    hol = mpp_calendars.CalendarException(date(2026, 12, 24), date(2026, 12, 26))
    hol.name = "Winter\nbreak"
    rec = mpp_calendars.Recurring(date(2026, 1, 3), date(2026, 12, 5), "MONTHLY", True, 12)
    rec.days, rec.day_number, rec.frequency = {6}, 1, 1
    monthly = mpp_calendars.CalendarException(None, None, rec)
    monthly.ranges = [(9 * 3600, 12 * 3600)]
    ann.exceptions = [hol, monthly]
    bob = mpp_calendars.default_derived()
    bob.uid, bob.name = 3, "Bob"
    bob.set_parent(std)
    p = project("T")
    p.calendars, p.default_calendar = [std, ann, bob], std
    return p


def test_calendars_listed():
    ctx = Context()
    doc = mpp.build(calendar_project(), ctx)
    md = render(doc, ctx)
    assert [b.inlines[0].text for b in doc.blocks if isinstance(b, Heading)] == ["Tasks", "Calendars"]
    lines = md.split("## Calendars\n\n")[1].splitlines()
    assert lines == ["- **Standard** (default): Mon 22:00-24:00; Tue-Fri 08:00-12:00, 13:00-17:00; Sat-Sun non-working",
                     "- **Ann** (based on Standard): Fri non-working",
                     "  - the 1st Saturday of every month, 2026-01-03 to 2026-12-05, 12 times: 09:00-12:00",
                     "  - 2026-12-24 to 2026-12-26 (Winter break): non-working"], lines
    assert not ctx.skipped.get("record") and not ctx.skipped.get("sheet_cut")


def test_recurrence_and_days_text():
    r = mpp_calendars.Recurring(date(2026, 1, 1), date(2026, 3, 1), "WEEKLY", False, 0)
    r.days, r.frequency = {0, 3}, 1
    assert mpp.recurrence_text(r) == "every week on Wed, Sun, 2026-01-01 to 2026-03-01"
    r.kind, r.frequency = "DAILY", 3
    assert mpp.recurrence_text(r) == "every 3 days, 2026-01-01 to 2026-03-01"
    r.kind, r.relative, r.day_number, r.month = "YEARLY", False, 4, 7
    assert mpp.recurrence_text(r) == "every year on July 4, 2026-01-01 to 2026-03-01"
    r.relative, r.days, r.day_number = True, {4}, 5
    assert mpp.recurrence_text(r) == "the last Thursday of July every year, 2026-01-01 to 2026-03-01"
    r.kind, r.relative, r.day_number, r.frequency = "MONTHLY", False, 15, 2
    assert mpp.recurrence_text(r) == "day 15 of every 2 months, 2026-01-01 to 2026-03-01"
    w = mpp_calendars.Week()
    assert mpp.days_text(w, True) == ""
    w.set_working(0, False)
    w.set_type(3, "WORKING")
    w.hours[3] = [(1800, 3630)]
    assert mpp.days_text(w, True) == "Wed 00:30-01:00:30; Sun non-working"


def test_calendars_share_the_character_budget():
    """Calendar items are charged against the Budget's characters, past them the list stops (sheet_cut once);
    calendars that could not be read are counted `record`."""
    ctx = Context()
    items = mpp._calendars(calendar_project(), ctx, sheet_grid.Budget(chars=100))
    assert len(items) == 1 and ctx.skipped["sheet_cut"] == 1, items
    assert "calendars cut at 100 characters" in ctx.warnings
    p = project("T")
    p.calendars = None
    ctx = Context()
    md = render(mpp.build(p, ctx), ctx)
    assert "Calendars" not in md and ctx.skipped["record"] == 1 and "calendars could not be read" in ctx.warnings


def test_duration_relation_resource_text():
    d = mpp.duration_text
    assert d((5.0, "DAYS")) == "5d" and d((2.5, "HOURS")) == "2.5h" and d((3.0, "ELAPSED_DAYS")) == "3ed"
    for unit, suffix in (("MINUTES", "m"), ("HOURS", "h"), ("DAYS", "d"), ("WEEKS", "w"), ("MONTHS", "mo"),
                         ("YEARS", "y"), ("PERCENT", "%"), ("ELAPSED_MINUTES", "em"), ("ELAPSED_HOURS", "eh"),
                         ("ELAPSED_DAYS", "ed"), ("ELAPSED_WEEKS", "ew"), ("ELAPSED_MONTHS", "emo"),
                         ("ELAPSED_YEARS", "ey"), ("ELAPSED_PERCENT", "e%")):
        assert d((4.0, unit)) == "4" + suffix, unit
    assert d((0.0, "DAYS")) == "0d" and d((-0.0, "DAYS")) == "0d" and d((-2.0, "DAYS")) == "-2d"
    assert d((0.1 + 0.2, "DAYS")) == "0.3d" and d((1.25, "WEEKS")) == "1.25w" and d((7, "DAYS")) == "7d"
    assert d((5.0, "FORTNIGHTS")) == "5 fortnights" and d((5.0, "")) == "5" and d((5.0, None)) == "5"
    assert d((5.0, "x" * 33)) == "5" and d((5.0, "DAYS\n| x")) == "5" and d((5.0, "days")) == "5"
    assert d((math.nan, "DAYS")) == "" and d((math.inf, "DAYS")) == "" and d((-math.inf, "HOURS")) == ""
    assert d((None, "DAYS")) == "" and d(("5", "DAYS")) == "" and d(None) == "" and d("3 days?") == "3 days?"
    assert d((1e300, "DAYS")) == "1e+300d" and len(d((1.7976931348623157e308, "DAYS"))) < 30
    assert d((True, "DAYS")) == ""

    r = mpp.relation_text
    assert r(3, "FINISH_START", (2.0, "DAYS")) == "3FS+2d" and r(4, "START_START", (0.0, "DAYS")) == "4SS"
    assert r(3, "FINISH_START", (-1.0, "DAYS")) == "3FS-1d"
    assert r(5, "FINISH_FINISH", (0.0, "DAYS")) == "5FF" and r(6, "START_FINISH", (1.5, "HOURS")) == "6SF+1.5h"
    assert r(7, "FINISH_START", (50.0, "PERCENT")) == "7FS+50%" and r(7, "FINISH_START", (-0.0, "DAYS")) == "7FS"
    assert r(8, "FINISH_START", (3.0, "ELAPSED_DAYS")) == "8FS+3ed"
    assert r(9, "SOMETHING_NEW", (0.0, "DAYS")) == "9" and r(9, None, (1.0, "DAYS")) == "9+1d"
    assert r(10, "FINISH_START", (math.nan, "DAYS")) == "10FS" and r(10, "FINISH_START", (math.inf, "DAYS")) == "10FS"
    assert r(11, "FINISH_START", (2.0, "FORTNIGHTS")) == "11FS+2 fortnights"
    assert r(12, "FINISH_START", (-1e300, "DAYS")) == "12FS-1e+300d" and r(0, "FINISH_START", None) == "0FS"

    s = mpp.resource_text
    assert s("Ann", 50.0) == "Ann[50%]" and s("Ann", 100.0) == "Ann" and s("Ann", 100) == "Ann"
    assert s("Ann", 0.0) == "Ann[0%]" and s("Ann", 200.0) == "Ann[200%]" and s("Ann", 12.5) == "Ann[12.5%]"
    assert s("Ann", math.nan) == "Ann" and s("Ann", None) == "Ann" and s("Ann", math.inf) == "Ann"
    assert s("Ann", -50.0) == "Ann[-50%]" and s("", 50.0) == "[50%]" and s(None, 100.0) == ""


NOTES = (
    ("", ""),
    (None, ""),
    ("{\\rtf1 a\\line b}", "a\nb"),
    ("{\\rtf1 a{\\cell b{\\i d}\\par e\\tab}c}", "a\tbd\ne\tc"),
    ("{\\rtf1 a\\cellx b}", "ab"),
    ("{\\rtf1 a{\\*\\fldrslt b}c}", "abc"),
    ("{\\rtf1\\mac \\'8e\\par}", "é"),
    ("{\\rtf1\\pc \\'82\\par}", "é"),
    ("{\\rtf1\\ansi\\ansicpg949 \\'b0\\'a1\\par}", "가"),
    ("{\\rtf1\\ansi\\ansicpg1251 \\'cf\\par}", "П"),
    ("{\\rtf1\\ansi\\ansicpg65001 \\'ea\\'b0\\'80\\par}", "가"),
    ("{\\rtf1\\ansi\\deff0{\\fonttbl{\\f0\\fcharset129 G;}} \\'b0\\'a1\\par}", " 가"),
    ("{\\rtf1\\ansi{\\fonttbl\\f0\\fcharset204 A;\\f1\\fcharset129 B;}\\f0\\'cf\\f1\\'b0\\'a1\\par}", "П가"),
    ("{\\rtf1\\ansi{\\fonttbl{\\f0\\fcharset129 A;}}\\f0\\'b0{\\'a1}\\par}", "��"),
    ("{\\rtf1 {\\fonttbl{\\f0\\fcharset129 G;}}{\\fonttbl{\\f0\\fcharset204 G;}}\\f0\\'cf\\par}", "П"),
    ("{\\rtf1 \\uc2\\u54620\\'41 x\\par}", "한x"),
    ("{\\rtf1 {\\uc2 \\u54620 ab}{\\u54620 cd}\\par}", "한한d"),
    ("{\\rtf1 \\uc2\\u54620\\emdash x\\par}", "한—"),
    ("{\\rtf1 \\uc2\\u54620\r\nxyz\\par}", "한z"),
    ("{\\rtf1 \\uc2\\u54620\\u44544 xy\\par}", "한글"),
    ("{\\rtf1 a{\\upr{b}{\\*\\ud{c}}}d}", "acd"),
    ("{\\rtf1 a{\\upr{\\fonttbl b}{\\*\\ud{c\\par}}}d}", "ac\nd"),
    ("{\\rtf1 {\\listtext 1.\\tab}item\\par}", "\titem"),
    ("{\\rtf1 {\\*\\unknown hidden}vis\\par}", "hiddenvis"),
    ("{\\rtf1 {\\header head}body\\par}", "body"),
    ("{\\rtf1 \\'7b\\{\\}\\\\x\\par}", "{{}\\x"),
    (" {\\rtf1 a\\par}", " a\n"),
    ("\r\n{\\rtf1 a\\par}", "a\n"),
)
DIVERGED = (
    ("plain text\r\nkept", "plain text\r\nkept"),
    ("{\\rtf1\\ansi\\ansicpg949 \\'81\\'41\\par}", "갂"),
    ("{\\rtf1 한글\\par}", "한글"),
    ("{\\rtf1 a{\\i b\\par}", "ab"),
    ("{\\rtf1 a}}b{\\par c}", "ab\nc"),
    ("{\\rtf1 a\\bin3 xyz b}", "a b"),
    ("{\\rtf1\\ansi\\ansicpg9999 \\'41\\par}", "A"),
)


def test_notes_text():
    for raw, want in NOTES:
        assert mpp_notes.text(raw) == want, (raw, mpp_notes.text(raw), want)


def test_notes_text_diverges_for_fidelity():
    """Where RtfHelper.strip loses text (a note that is no RTF, Java's IBM949 table, byte-cast characters past 0xFF)
    or throws (unbalanced braces, \\bin, an unknown code page), the text is kept or read as far as it goes."""
    for raw, want in DIVERGED:
        assert mpp_notes.text(raw) == want, (raw, mpp_notes.text(raw), want)


def task(tid, name, *, wbs=None, level=1, dur=None, start=None, finish=None, pct=None, summary=False,
         milestone=False, notes=None, null=False, active=True, preds=None):
    t = mpp_read.Task()
    t.f.update(id=tid, unique_id=tid, name=name, wbs=wbs, outline_level=level, duration=dur, percent=pct,
               start=None if start is None else datetime.fromisoformat(start),
               finish=None if finish is None else datetime.fromisoformat(finish), notes=notes, milestone=milestone)
    if not active:
        t.f["active"] = False
    t.null, t.summary = null, summary
    return t


def resource(rid, name, kind="WORK", max_units=100.0, active=True):
    r = mpp_read.Resource()
    r.f.update(id=rid, unique_id=rid, name=name)
    if not active:
        r.f["active"] = False
    r.kind, r.max_units = kind, max_units
    return r


def project(title=None, start=None, finish=None, status=None, tasks=(), resources=()):
    p = mpp_read.Project()
    p.title = title
    p.start, p.finish, p.status = (None if v is None else datetime.fromisoformat(v) for v in (start, finish, status))
    p.tasks = list(tasks)
    p.resources = list(resources)
    for r in resources:
        p.resource_uids[r.uid] = r
    return p


def assign(p, t, r, units=100.0):
    a = mpp_read.Assignment(None if r is None else r.uid)
    a.units = units
    p.assigned.setdefault(t.uid, []).append(a)


def link(p, succ, pred, kind="FINISH_START", lag=None):
    p.preds.setdefault(succ, []).append(mpp_read.Relation(pred, kind, lag))


def sample_project():
    ann, bob = resource(1, "Ann"), resource(2, "Bob", "MATERIAL", 300.0)
    t0 = task(0, "Launch", level=0, summary=True, dur=(20.0, "DAYS"), start="2026-01-05", finish="2026-03-27",
              pct=25.0)
    t1 = task(1, "Design", wbs="1.1", dur=(5.0, "DAYS"), start="2026-01-05", finish="2026-01-09", pct=100.0,
              notes="Check the\nbrief")
    t2 = task(2, "Build", wbs="1.2", dur=(2.5, "HOURS"), start="2026-01-12", finish="2026-01-12")
    t3 = task(3, "Go live", wbs="1.3", dur=(0.0, "DAYS"), milestone=True, start="2026-03-27", finish="2026-03-27")
    gone = task(None, None, null=True, notes="never shown")
    off = task(4, "Parked", wbs="1.4", active=False, notes="old idea")
    p = project("Launch plan", "2026-01-05", "2026-03-27", "2026-02-01", [t0, t1, gone, t2, t3, off],
                [resource(0, None), ann, bob, resource(6, "Idle", active=False)])
    assign(p, t1, ann, 50.0)
    assign(p, t1, bob)
    assign(p, t1, None)
    link(p, t2, t1, "FINISH_START", (2.0, "DAYS"))
    link(p, t2, t0, "START_START", (0.0, "DAYS"))
    link(p, t2, t1, "FINISH_FINISH", (-1.0, "DAYS"))
    link(p, t2, task(None, "no ID"))
    return p


def test_build_output():
    ctx = Context()
    doc = mpp.build(sample_project(), ctx)
    assert doc.title == "Launch plan"
    assert doc.blocks[0] == Para([Text("Start 2026-01-05 · Finish 2026-03-27 · Status date 2026-02-01")])
    heads = [b.inlines[0].text for b in doc.blocks if isinstance(b, Heading)]
    assert heads == ["Tasks", "Resources", "Notes"]
    md = render(doc, ctx)
    lines = md.splitlines()
    assert HEADER in md, md
    assert "|0|0|**Launch**|20d|2026-01-05|2026-03-27|25%|||" in lines, md
    assert "1|1.1|Design|5d|2026-01-05|2026-01-09|100%||Ann[50%], Bob" in lines, md
    assert "|2|1.2|Build|2.5h|2026-01-12|2026-01-12||1FS+2d, 0SS, 1FF-1d||" in lines, md
    assert "|3|1.3|Go live (milestone)|0d|2026-03-27|2026-03-27||||" in lines, md
    assert "Parked" not in md and "never shown" not in md and "old idea" not in md
    assert "ID|Name|Type|Max units" in lines
    assert "1|Ann|Work|100%" in lines and "2|Bob|Material|300%" in lines, md
    assert "Idle" not in md and "0||Work|100%" not in lines
    assert "- 1 Design: Check the" in md and "brief" in md, md
    shown = render(doc, Context(), hidden=True)
    assert "Parked" in shown and "4 Parked: old idea" in shown and "Idle" in shown
    assert not ctx.skipped.get("text") and not ctx.skipped.get("record") and not ctx.skipped.get("sheet_cut")
    tables = [b for b in doc.blocks if isinstance(b, Table)]
    assert len(tables) == 2 and tables[0].hidden_rows == frozenset({5}), tables[0].hidden_rows
    name0 = tables[0].rows[1][2].blocks[0].inlines
    assert isinstance(name0[0], Bold) and name0[0].inlines == [Text("Launch")]
    assert tables[0].rows[4][2].blocks[0].inlines[-1] == Text(" (milestone)")


def test_baseline_columns_shown_when_a_task_has_a_baseline():
    md = render(mpp.build(sample_project(), Context()), Context())
    assert "Baseline" not in md, md
    p = sample_project()
    t = p.tasks[1]
    t.f.update(baseline_start=datetime(2026, 1, 5, 8), baseline_finish=datetime(2026, 1, 7, 17),
               baseline_duration=(3.0, "DAYS"))
    p.tasks[2].f["baseline_duration"] = (0.0, "DAYS")
    md = render(mpp.build(p, Context()), Context())
    assert "Baseline" not in md, md
    p.baseline = True
    md = render(mpp.build(p, Context()), Context())
    lines = md.splitlines()
    assert ("ID|Outline|Name|Duration|Start|Finish|Baseline duration|Baseline start|Baseline finish|% Complete|"
            "Predecessors|Resources") in lines, md
    assert "1|1.1|Design|5d|2026-01-05|2026-01-09|3d|2026-01-05|2026-01-07|100%||Ann[50%], Bob" in lines, md
    assert "|2|1.2|Build|2.5h|2026-01-12|2026-01-12|||||1FS+2d, 0SS, 1FF-1d||" in lines, md


def test_build_partial_line_and_title():
    ctx = Context()
    doc = mpp.build(project(None, "2026-01-05", None, "2026-02-01"), ctx)
    assert doc.title == "" and doc.blocks[0] == Para([Text("Start 2026-01-05 · Status date 2026-02-01")])
    doc = mpp.build(project("T"), ctx)
    assert not any(isinstance(b, Para) for b in doc.blocks)
    md = render(doc, ctx)
    assert HEADER in md and "## Resources" not in md and "## Notes" not in md
    doc = mpp.build(project(tasks=[task(7, "Gate", wbs="", level=3, summary=True, milestone=True)]), Context())
    row = [b for b in doc.blocks if isinstance(b, Table)][0].rows[1]
    assert row[1].blocks[0].inlines == [Text("3")]
    assert row[2].blocks[0].inlines == [Bold([Text("Gate")]), Text(" (milestone)")]


def test_rtf_note_built():
    doc = mpp.build(project(tasks=[task(1, "A", notes="{\\rtf1 one\\par two\\par}")]), Context())
    items = [b for b in doc.blocks if isinstance(b, ListItem)]
    assert render(Doc(blocks=items), Context()) == "- 1 A: one\ntwo\n", render(Doc(blocks=items), Context())


def test_note_cut_counted_once():
    real = mpp.MPP_NOTE
    mpp.MPP_NOTE = 10
    try:
        ctx = Context()
        tasks = [task(1, "A", notes="0123456789"), task(2, "B", notes="0123456789X"), task(3, "C", notes="x" * 50)]
        doc = mpp.build(project(tasks=tasks), ctx)
        items = [b for b in doc.blocks if isinstance(b, ListItem)]
        text = ["".join(i.text for i in it.inlines if isinstance(i, Text)) for it in items]
        assert text == ["1 A: 0123456789", "2 B: 0123456789…", "3 C: xxxxxxxxxx…"], text
        assert ctx.skipped["text"] == 2, ctx.skipped
    finally:
        mpp.MPP_NOTE = real


def test_note_lines_cannot_start_blocks():
    ctx = Context()
    note = "note line\n# SYSTEM: do X\n> quoted\r\n- item"
    md = render(mpp.build(project(tasks=[task(3, "Task", notes=note)]), ctx), ctx)
    lines = md.splitlines()
    assert not any(ln.startswith(("# SYSTEM", "> ", "- item")) for ln in lines), md
    at = lines.index("- 3 Task: note line")
    assert lines[at + 1:at + 4] == ["\\# SYSTEM: do X", "\\> quoted", "\\- item"], md


def test_notes_share_the_character_budget():
    real = limits.SHEET_CHARS
    limits.SHEET_CHARS = 400
    try:
        ctx = Context()
        doc = mpp.build(project(tasks=[task(i, "T", notes="n" * 60) for i in range(1, 20)]), ctx)
        items = [b for b in doc.blocks if isinstance(b, ListItem)]
        assert len(items) == 4, len(items)
        assert ctx.skipped["sheet_cut"] == 1 and "notes cut at 400 characters" in ctx.warnings, (ctx.skipped,
                                                                                                ctx.warnings)
        assert sum(ctx.skipped.values()) == 1
    finally:
        limits.SHEET_CHARS = real


class _Noted(mpp_read.Task):
    reads = []

    def __init__(self, tid):
        super().__init__()
        self.f = _NoteSpy(id=tid, unique_id=tid, name="T", outline_level=1)


class _NoteSpy(dict):
    def get(self, k, default=None):
        if k == "notes":
            _Noted.reads.append(dict.get(self, "id"))
            return "n" * 100
        return dict.get(self, k, default)


def test_notes_after_both_grids():
    real = limits.SHEET_CHARS
    limits.SHEET_CHARS = 300
    try:
        ctx = Context()
        doc = mpp.build(project(tasks=[task(i, "T", notes="n" * 100) for i in range(1, 6)],
                                resources=[resource(1, "Ann"), resource(2, "Bob")]), ctx)
        md = render(doc, ctx)
        assert all(f"|{i}|1|T|" in md for i in range(1, 6)), md
        assert "\n1|Ann|Work|100%\n" in md and "\n2|Bob|Work|100%\n" in md, md
        items = [b for b in doc.blocks if isinstance(b, ListItem)]
        assert len(items) == 1 and ctx.skipped["sheet_cut"] == 1, (len(items), ctx.skipped)
        heads = [b.inlines[0].text for b in doc.blocks if isinstance(b, Heading)]
        assert heads == ["Tasks", "Resources", "Notes"]
        _Noted.reads.clear()
        mpp.build(project(tasks=[_Noted(i) for i in range(1, 6)]), Context())
        assert _Noted.reads == [1, 2, 3], _Noted.reads
    finally:
        limits.SHEET_CHARS = real


def test_rows_and_cells_capped():
    real = limits.SHEET_ROWS
    limits.SHEET_ROWS = 4
    try:
        ctx = Context()
        doc = mpp.build(project(tasks=[task(i, f"T{i}", notes=f"note {i}") for i in range(1, 50)],
                                resources=[resource(i, f"R{i}") for i in range(1, 50)]), ctx)
        md = render(doc, ctx)
        assert "\n|3|1|T3|||||||\n" in md and "T4" not in md, md
        assert "note 3" in md and "note 4" not in md
        assert ctx.skipped["sheet_cut"] == 2, ctx.skipped
    finally:
        limits.SHEET_ROWS = real
    real = limits.SHEET_CELLS
    limits.SHEET_CELLS = 9 + 9 + 4
    try:
        ctx = Context()
        tasks = [task(i, f"T{i}", dur=(1.0, "DAYS"), start="2026-01-01", finish="2026-01-02", pct=0.0, wbs=str(i),
                      notes=f"note {i}") for i in range(1, 50)]
        doc = mpp.build(project(tasks=tasks, resources=[resource(1, "R1")]), ctx)
        assert ctx.skipped["sheet_cut"] == 2, ctx.skipped
        md = render(doc, ctx)
        assert "T1" in md and "T3" not in md
        assert "note 1" in md and "note 2" not in md, md
        limits.SHEET_CELLS = 3
        seen = []
        ctx = Context()
        mpp.build(project(tasks=[_Spy(seen, 1), _Spy(seen, 2)]), ctx)
        assert seen == [] and ctx.skipped["sheet_cut"] == 1, (seen, ctx.skipped)
    finally:
        limits.SHEET_CELLS = real


class _Spy(mpp_read.Task):
    """A task that records each time its null flag is read."""

    def __init__(self, seen, tid=None, null=False):
        super().__init__()
        self.seen, self._null = seen, null
        self.f.update(id=tid, unique_id=tid)

    @property
    def null(self):
        self.seen.append(1)
        return self._null

    @null.setter
    def null(self, v):
        pass


class _SpyRes(mpp_read.Resource):
    def __init__(self, seen, rid):
        super().__init__()
        self.seen = seen
        self.f = _GetSpy(seen, id=0, name="")


class _GetSpy(dict):
    def __init__(self, seen, **kw):
        super().__init__(**kw)
        self.seen = seen

    def get(self, k, default=None):
        if k == "id":
            self.seen.append(1)
        return dict.get(self, k, default)


def test_rows_capped_without_values():
    real = limits.SHEET_ROWS
    limits.SHEET_ROWS = 5
    try:
        seen = []
        ctx = Context()
        mpp.build(project(tasks=[_Spy(seen) for _ in range(1000)]), ctx)
        assert len(seen) == 4 and ctx.skipped["sheet_cut"] == 1, (len(seen), ctx.skipped)
        seen.clear()
        ctx = Context()
        mpp.build(project(tasks=[_Spy(seen) for _ in range(4)]), ctx)
        assert len(seen) == 4 and not ctx.skipped, (len(seen), ctx.skipped)
        seen.clear()
        ctx = Context()
        doc = mpp.build(project(resources=[_SpyRes(seen, i) for i in range(1000)]), ctx)
        assert len(seen) == 4 and ctx.skipped["sheet_cut"] == 1, (len(seen), ctx.skipped)
        assert not any(isinstance(b, Heading) and b.inlines[0].text == "Resources" for b in doc.blocks)
        seen.clear()
        ctx = Context()
        mpp.build(project(resources=[_SpyRes(seen, i) for i in range(4)]), ctx)
        assert len(seen) == 4 and not ctx.skipped, (len(seen), ctx.skipped)
    finally:
        limits.SHEET_ROWS = real


class _Many(mpp_read.Project):
    """A project whose one task has a million predecessors or assignments, made as they are read."""

    def __init__(self, reads, make):
        super().__init__()
        self.reads, self.make = reads, make

    def _gen(self, assignments):
        if (self.make is _asg) != assignments:
            return
        for i in range(1_000_000):
            self.reads.append(i)
            yield self.make(self, i)

    def predecessors(self, t):
        return self._gen(False)

    def assignments(self, t):
        return self._gen(True)


def _rel(p, i):
    return mpp_read.Relation(task(i, "x"), "FINISH_START", None)


def _unnamed(p, i):
    return mpp_read.Relation(task(None, "x"), "FINISH_START", None)


def _asg(p, i):
    p.resource_uids[i] = resource(i, "Resource name")
    return mpp_read.Assignment(i)


def test_join_bounded():
    real = limits.SHEET_CHARS
    limits.SHEET_CHARS = 200
    try:
        for make, most in ((_rel, 100), (_asg, 100), (_unnamed, 1000)):
            reads = []
            p = _Many(reads, make)
            p.tasks = [task(1, "Big")]
            ctx = Context()
            mpp.build(p, ctx)
            assert len(reads) < most, (make.__name__, len(reads))
            if make is _rel:
                assert ctx.skipped["sheet_cut"] == 1
    finally:
        limits.SHEET_CHARS = real


class _Broken(dict):
    def get(self, k, default=None):
        raise RuntimeError("a field that cannot be read")


def test_task_guarded():
    ctx = Context()
    bad = task(2, "B", notes="lost")
    p = project(tasks=[task(1, "A"), bad, task(3, "C")])
    bad.f = _Broken(bad.f)
    md = render(mpp.build(p, ctx), ctx)
    assert "\n|1|1|A|||||||\n|3|1|C|||||||\n" in md and "lost" not in md, md
    assert ctx.skipped["record"] == 1
    ctx = Context()
    broken = resource(2, "x")
    p = project(resources=[resource(1, "Ann"), broken, resource(3, "Cy")])
    broken.f = _Broken(broken.f)
    md = render(mpp.build(p, ctx), ctx)
    assert "\n1|Ann|Work|100%\n" in md and "\n3|Cy|Work|100%\n" in md and ctx.skipped["record"] == 1, md


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
