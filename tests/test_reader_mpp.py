"""Microsoft Project (mpp): unsupported without the MPXJ venv, routing by the CompObj ProgID, the read.py engine and
re-exec, the pure cell helpers, the Doc built from a stand-in MPXJ project (no JPype), read() against stand-in jpype and
mpxj modules, and the venv-only reads of MPXJ's own test files (skipped, saying why, without a ready venv).

Run: python -I -X utf8 tests/test_reader_mpp.py
"""
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE / "scripts"))
sys.path.insert(0, str(Path(__file__).parent))

import read  # noqa: E402
import reader  # noqa: E402
from reader import limits, markdown, pyenv  # noqa: E402
from reader.containers.cfb import Compound  # noqa: E402
from reader.formats import mpp, mpp_mpxj  # noqa: E402
from reader.limits import ReadError  # noqa: E402
from reader.model import Bold, Context, Doc, Heading, ListItem, Para, Table, Text  # noqa: E402
from reader_fix import cfb_bytes, comp_obj  # noqa: E402

UNSUPPORTED = "Microsoft Project files are read only through MPXJ"
MSPROJECT = comp_obj(b"MSProject.MPP9", b"Microsoft Project")
HEADER = "|ID|Outline|Name|Duration|Start|Finish|% Complete|Predecessors|Resources|"
READ_PY = HERE / "scripts" / "read.py"


def raises(exc, fn, *a, **kw):
    try:
        fn(*a, **kw)
    except exc as e:
        return e
    raise AssertionError(f"{exc.__name__} not raised")


def project_file(d, streams=None, name="plan.mpp"):
    """A clean compound file (a CompObj naming MS Project unless `streams` says otherwise) at d/name."""
    p = Path(d) / name
    p.write_bytes(cfb_bytes({"\x01CompObj": MSPROJECT} if streams is None else streams)[0])
    return p


def cli(*args):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = read.main(list(args))
    return code, buf.getvalue()


class env_var:
    """Set (or unset, with None) environment variables for a block, restored after."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)

    def __exit__(self, *exc):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- mpp.parse without the engine, and routing (N20)

def test_no_engine_unsupported():
    with tempfile.TemporaryDirectory() as d:
        p = project_file(d)
        e = raises(ReadError, mpp.parse, p, Context())
        assert e.kind == "unsupported" and e.detail == UNSUPPORTED, e
        e = raises(ReadError, mpp.parse, p, Context(), engine=None)
        assert e.kind == "unsupported" and e.detail == UNSUPPORTED


def test_not_msproject_routes():
    # a compound file whose CompObj does not name MS Project goes to cfb_route (as ppt's parse_compound)
    assert mpp.route(cfb_bytes({"\x01CompObj": MSPROJECT})[0]) is None
    # the CompObj is asked first: a Project file with another format's stream is still Project's
    assert mpp.route(cfb_bytes({"\x01CompObj": MSPROJECT, "WordDocument": bytes(64)})[0]) is None
    assert mpp.route(cfb_bytes({"VisioDocument": b"Visio (TM) Drawing\r\n"})[0]) == "vsd"
    assert mpp.route(cfb_bytes({"WordDocument": bytes(64), "\x01CompObj": comp_obj(b"Word.Document.8")})[0]) == "doc"
    for streams in ({"Other": b"x"}, {"\x01CompObj": comp_obj(b"Word.Document.8")}, {"\x01CompObj": b"\x01" * 9}):
        e = raises(ReadError, mpp.route, cfb_bytes(streams)[0])
        assert e.kind == "damaged" and e.detail == "no MSProject CompObj", (streams, e)
    e = raises(ReadError, mpp.route, cfb_bytes({"EncryptedPackage": b"x"})[0])
    assert e.kind == "encrypted"
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "x.mpp"
        p.write_bytes(b"not a compound file at all")
        e = raises(ReadError, mpp.parse, p, Context())
        assert e.kind == "damaged" and e.detail == "not a Microsoft Project file"
        # the engine never sees a renamed compound file of another format
        p = project_file(d, {"Other": b"x"}, "y.mpp")
        e = raises(ReadError, mpp.parse, p, Context(), engine="mpxj")
        assert e.kind == "damaged" and e.detail == "no MSProject CompObj"


def test_zip_handed_to_zip_route():
    # a zip named .mpp goes to zip_route, which picks its parser (never mpp, never MPXJ)
    import zipfile
    seen = []
    real = mpp.zip_route.parse

    def fake(path, ctx):
        seen.append(Path(path).name)
        return Doc(title="zip")

    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "z.mpp"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("[Content_Types].xml", "<Types/>")
        e = raises(ReadError, mpp.parse, p, Context())  # the real zip_route: not a Project file's refusal
        assert e.detail not in (mpp.UNSUPPORTED, "not a Microsoft Project file"), e
        mpp.zip_route.parse = fake
        try:
            assert mpp.parse(p, Context()).title == "zip" and mpp.parse(p, Context(), engine="mpxj").title == "zip"
        finally:
            mpp.zip_route.parse = real
        assert seen == ["z.mpp", "z.mpp"]


def test_broken_fat_damaged():
    # a compound file whose FAT or mini FAT is broken (in memory): a clean damaged, never another exception
    import struct
    for streams in ({"\x01CompObj": MSPROJECT}, {"\x01CompObj": MSPROJECT, "Big": bytes(9000)}):
        data, info = cfb_bytes(streams)
        b = bytearray(data)
        for sid in info["fat"]:  # every FAT entry names sector 5: the directory chain loops
            b[(sid + 1) * 512:(sid + 2) * 512] = struct.pack("<I", 5) * 128
        e = raises(ReadError, mpp.route, bytes(b))
        assert e.kind == "damaged", e
        b = bytearray(data)
        first_mini = struct.unpack_from("<I", b, 0x3C)[0]
        for i in range(128):  # every mini FAT entry names itself: the CompObj's mini chain loops
            struct.pack_into("<I", b, (first_mini + 1) * 512 + 4 * i, i)
        e = raises(ReadError, mpp.route, bytes(b))
        assert e.kind == "damaged" and e.detail == "no MSProject CompObj", e


def test_not_msproject_handed_on():
    from reader.formats import cfb_route
    seen = []
    real = cfb_route.parse

    def fake(fmt, path, ctx):
        seen.append(fmt)
        return Doc(title=fmt)

    cfb_route.parse = fake
    try:
        with tempfile.TemporaryDirectory() as d:
            p = project_file(d, {"VisioDocument": b"Visio (TM) Drawing\r\n"}, "v.mpp")
            assert mpp.parse(p, Context()).title == "vsd" and mpp.parse(p, Context(), engine="mpxj").title == "vsd"
            assert seen == ["vsd", "vsd"]
    finally:
        cfb_route.parse = real


def test_engine_mpxj_calls_reader():
    seen = []
    real = mpp_mpxj.read

    def fake(path, ctx):
        seen.append(Path(path).name)
        return Doc(title="T")

    mpp_mpxj.read = fake
    try:
        with tempfile.TemporaryDirectory() as d:
            p = project_file(d)
            assert mpp.parse(p, Context(), engine="mpxj").title == "T" and seen == ["plan.mpp"]
            raises(ReadError, mpp.parse, p, Context())  # no engine: never the reader
            assert seen == ["plan.mpp"]
    finally:
        mpp_mpxj.read = real


def test_import_light():
    # the gate and the suite never import jpype or mpxj: mpp_mpxj imports them inside read only
    code = ("import sys; sys.path.insert(0, sys.argv[1]); import reader.formats.mpp, reader.formats.mpp_mpxj; "
            "print(sorted(m for m in ('jpype', 'mpxj') if m in sys.modules))")
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", "-c", code, str(HERE / "scripts")],
                       capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert r.returncode == 0 and r.stdout.strip() == "[]", (r.stdout, r.stderr)


# ---- read.py

def test_cli_without_venv():
    assert {"mpp", "mpt"} <= reader.BUILT
    with tempfile.TemporaryDirectory() as d, env_var(PHRO_READER_MPPENV=Path(d) / "missing"):
        p = project_file(d, name="x.mpp")
        code, out = cli("convert", str(p), "--out", str(Path(d) / "out"))
        assert code == 1 and out.strip() == f"FAIL\tunsupported\t{UNSUPPORTED}", (code, out)
        # the same through read.py in its own process, as the gate's child runs it
        r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(READ_PY), "convert", str(p), "--out",
                            str(Path(d) / "out2")], capture_output=True, text=True, encoding="utf-8", timeout=60,
                           env=dict(os.environ, PHRO_READER_CACHE=str(Path(d) / "cache")))
        assert r.returncode == 1 and r.stdout == f"FAIL\tunsupported\t{UNSUPPORTED}\n", (r.stdout, r.stderr)
        # --engine mpxj outside the venv: MPXJ does not import here, so the engine is not used
        calls, real = [], mpp_mpxj.read
        mpp_mpxj.read = lambda path, ctx: calls.append(path)
        try:
            code, out = cli("convert", str(p), "--engine", "mpxj", "--out", str(Path(d) / "out"))
        finally:
            mpp_mpxj.read = real
        assert code == 1 and out.strip() == f"FAIL\tunsupported\t{UNSUPPORTED}" and calls == [], (code, out)


def test_pdf_with_engine_mpxj_reads_natively():
    import pdf_fix
    with tempfile.TemporaryDirectory() as d, env_var(PHRO_READER_PYENV="off"):
        src = Path(d) / "a.pdf"
        src.write_bytes(pdf_fix.simple_pdf([pdf_fix.text_ops("hello")]))
        code, out = cli("convert", str(src), "--engine", "mpxj", "--out", str(Path(d) / "o"))
        assert code == 0 and "hello" in (Path(d) / "o" / "out.md").read_text(encoding="utf-8"), out


def test_engine_id():
    assert read._engine_id("mpxj") == ""  # this interpreter has no mpp venv's ready.json
    real = pyenv.engine_id_from_module
    pyenv.engine_id_from_module = lambda kind="pdf": "mpxj-16.10.0-java21" if kind == "mpp" else "pdfium-x-1"
    try:
        assert read._engine_id("mpxj") == "mpxj-16.10.0-java21"
    finally:
        pyenv.engine_id_from_module = real


def test_cli_engine_mpxj_passes_engine():
    got = {}
    real_id, real_read = read._engine_id, mpp_mpxj.read

    def fake_read(path, ctx):
        got["read"] = Path(path).name
        return Doc(blocks=[Para([Text("read through MPXJ")])])

    read._engine_id = lambda wanted: "mpxj-16.10.0-java21" if wanted == "mpxj" else "native"
    mpp_mpxj.read = fake_read
    try:
        with tempfile.TemporaryDirectory() as d, env_var(PHRO_READER_MPPENV=Path(d) / "missing"):
            p = project_file(d, name="x.mpp")
            code, out = cli("convert", str(p), "--engine", "mpxj", "--out", str(Path(d) / "out"))
            assert code == 0 and got == {"read": "x.mpp"}, (code, out)
            assert "read through MPXJ" in (Path(d) / "out" / "out.md").read_text(encoding="utf-8")
    finally:
        read._engine_id, mpp_mpxj.read = real_id, real_read


def test_cli_reexec_ready():
    # a READY mpp venv (stub Java): the convert re-runs under its interpreter with --engine mpxj; off reads in-process
    import test_reader_pyenv as tp
    calls = []

    def fake_run(cmd, *a, **kw):
        calls.append(list(cmd))
        return types.SimpleNamespace(returncode=0, stdout=b"relayed\n", stderr=b"")

    with tp.env() as d, tp.which_java(tp.JAVA_EXE):
        v = tp.mpp_ready(d)
        os.environ["PHRO_READER_MPPENV"] = str(v)
        assert pyenv.state(v, "mpp") == "READY"
        p = project_file(d, name="x.mpp")
        real = subprocess.run
        subprocess.run = fake_run
        try:
            code, out = cli("convert", str(p), "--hidden", "--pages", "1-2")
            assert code == 0 and "relayed" in out and len(calls) == 1, (code, out, calls)
            cmd = calls[0]
            assert Path(cmd[0]) == pyenv.interpreter(v) and cmd[1:4] == ["-I", "-X", "utf8"]
            assert cmd[cmd.index("--engine") + 1] == "mpxj" and str(p) in cmd and "--hidden" in cmd
            assert "--pages" not in cmd  # pages are pdf's only
            calls.clear()
            code, out = cli("convert", str(p), "--engine", "mpxj", "--out", str(d / "o"))  # already the child's form
            assert calls == [] and code == 1
            os.environ["PHRO_READER_MPPENV"] = "off"
            code, out = cli("convert", str(p), "--out", str(d / "o"))
            assert calls == [] and code == 1 and out.strip() == f"FAIL\tunsupported\t{UNSUPPORTED}", out
        finally:
            subprocess.run = real


# ---- the pure helpers

def test_duration_relation_resource_text():
    d = mpp_mpxj.duration_text
    assert d(5.0, "DAYS") == "5d" and d(2.5, "HOURS") == "2.5h" and d(3.0, "ELAPSED_DAYS") == "3ed"
    for unit, suffix in (("MINUTES", "m"), ("HOURS", "h"), ("DAYS", "d"), ("WEEKS", "w"), ("MONTHS", "mo"),
                         ("YEARS", "y"), ("PERCENT", "%"), ("ELAPSED_MINUTES", "em"), ("ELAPSED_HOURS", "eh"),
                         ("ELAPSED_DAYS", "ed"), ("ELAPSED_WEEKS", "ew"), ("ELAPSED_MONTHS", "emo"),
                         ("ELAPSED_YEARS", "ey"), ("ELAPSED_PERCENT", "e%")):
        assert d(4.0, unit) == "4" + suffix, unit
    assert d(0.0, "DAYS") == "0d" and d(-0.0, "DAYS") == "0d" and d(-2.0, "DAYS") == "-2d"
    assert d(0.1 + 0.2, "DAYS") == "0.3d" and d(1.25, "WEEKS") == "1.25w" and d(7, "DAYS") == "7d"
    # unknown units: the number and the unit's name, lower case; a name that is not a plain enum name is left out
    assert d(5.0, "FORTNIGHTS") == "5 fortnights" and d(5.0, "") == "5" and d(5.0, None) == "5"
    assert d(5.0, "x" * 33) == "5" and d(5.0, "DAYS\n| x") == "5" and d(5.0, "days") == "5"
    # NaN, infinite and huge amounts
    assert d(math.nan, "DAYS") == "" and d(math.inf, "DAYS") == "" and d(-math.inf, "HOURS") == ""
    assert d(None, "DAYS") == "" and d("5", "DAYS") == ""
    assert d(1e300, "DAYS") == "1e+300d" and d(-1e300, "DAYS") == "-1e+300d" and len(d(1.7976931348623157e308, "DAYS")) < 30
    assert d(10 ** 400, "DAYS") == "" and d(True, "DAYS") == ""

    r = mpp_mpxj.relation_text
    assert r(3, "FINISH_START", 2.0, "DAYS") == "3FS+2d" and r(4, "START_START", 0.0, "DAYS") == "4SS"
    assert r(3, "FINISH_START", -1.0, "DAYS") == "3FS-1d"
    assert r(5, "FINISH_FINISH", 0.0, "DAYS") == "5FF" and r(6, "START_FINISH", 1.5, "HOURS") == "6SF+1.5h"
    assert r(7, "FINISH_START", 50.0, "PERCENT") == "7FS+50%" and r(7, "FINISH_START", -0.0, "DAYS") == "7FS"
    assert r(8, "FINISH_START", 3.0, "ELAPSED_DAYS") == "8FS+3ed"
    assert r(9, "SOMETHING_NEW", 0.0, "DAYS") == "9" and r(9, None, 1.0, "DAYS") == "9+1d"
    assert r(10, "FINISH_START", math.nan, "DAYS") == "10FS" and r(10, "FINISH_START", math.inf, "DAYS") == "10FS"
    assert r(11, "FINISH_START", 2.0, "FORTNIGHTS") == "11FS+2 fortnights"
    assert r(12, "FINISH_START", 1e300, "DAYS") == "12FS+1e+300d" and r(12, "FINISH_START", -1e300, "DAYS") == "12FS-1e+300d"
    assert r(0, "FINISH_START", 0.0, None) == "0FS"

    s = mpp_mpxj.resource_text
    assert s("Ann", 50.0) == "Ann[50%]" and s("Ann", 100.0) == "Ann" and s("Ann", 100) == "Ann"
    assert s("Ann", 0.0) == "Ann[0%]" and s("Ann", 200.0) == "Ann[200%]" and s("Ann", 12.5) == "Ann[12.5%]"
    assert s("Ann", math.nan) == "Ann" and s("Ann", None) == "Ann" and s("Ann", math.inf) == "Ann"
    assert s("Ann", 1e300) == "Ann[1e+300%]" and s("Ann", -50.0) == "Ann[-50%]"
    assert s("", 50.0) == "[50%]" and s(None, 100.0) == ""


def test_refusal():
    p = "File is password protected"
    e = mpp_mpxj.refusal(p, p)
    assert e.kind == "encrypted" and e.detail == "Project password set"
    e = mpp_mpxj.refusal(p + ". Please enter password.", p)  # PASSWORD_PROTECTED_ENTER_PASSWORD (MPP9)
    assert e.kind == "encrypted" and e.detail == "Project password set"
    for msg in ("Invalid file format: MSProject.MPP4", "Error reading file", "", "file is password protected"):
        e = mpp_mpxj.refusal(msg, p)
        assert e.kind == "damaged" and "MPXJ" in e.detail and (not msg or msg not in e.detail), (msg, e)
    assert mpp_mpxj.refusal("anything", "").kind == "damaged"  # no prefix read from the class: never encrypted


# ---- the Doc, from a stand-in MPXJ project (the Java getters MPXJ 16 has, answered in Python)

class N:
    """java.lang.Number"""

    def __init__(self, v):
        self.v = v

    def doubleValue(self):
        return float(self.v)

    def intValue(self):
        return int(self.v)


class E:
    """a Java enum"""

    def __init__(self, n):
        self.n = n

    def name(self):
        return self.n


class Day:
    """java.time.LocalDateTime: toLocalDate() prints as YYYY-MM-DD"""

    def __init__(self, iso):
        self.iso = iso

    def toLocalDate(self):
        return self.iso


class Dur:
    def __init__(self, amount, unit="DAYS"):
        self.a, self.u = amount, unit

    def getDuration(self):
        return self.a

    def getUnits(self):
        return E(self.u)


class Rel:
    def __init__(self, task, kind="FINISH_START", lag=None):
        self.t, self.k, self.l = task, kind, lag

    def getPredecessorTask(self):
        return self.t

    def getType(self):
        return E(self.k)

    def getLag(self):
        return self.l


class Res:
    def __init__(self, rid, name, kind="WORK", max_units=100.0, null=False, active=True):
        self.rid, self.nm, self.k, self.mu, self.null, self.act = rid, name, kind, max_units, null, active

    def getID(self):
        return None if self.rid is None else N(self.rid)

    def getName(self):
        return self.nm

    def getType(self):
        return None if self.k is None else E(self.k)

    def getMaxUnits(self):
        return None if self.mu is None else N(self.mu)

    def getNull(self):
        return self.null

    def getActive(self):
        return self.act


class Asg:
    def __init__(self, res, units=100.0):
        self.r, self.u = res, units

    def getResource(self):
        return self.r

    def getUnits(self):
        return None if self.u is None else N(self.u)


class Task:
    def __init__(self, tid, name, *, wbs=None, level=1, dur=None, start=None, finish=None, pct=None, preds=(),
                 res=(), summary=False, milestone=False, notes=None, null=False, active=True, broken=False):
        self.__dict__.update(tid=tid, nm=name, wbs=wbs, level=level, dur=dur, start=start, finish=finish, pct=pct,
                             preds=preds, res=res, summary=summary, milestone=milestone, notes=notes,
                             null=null, active=active, broken=broken)

    def getID(self):
        if self.broken:
            raise RuntimeError("a getter MPXJ could not answer")
        return None if self.tid is None else N(self.tid)

    def getWBS(self):
        return self.wbs

    def getOutlineLevel(self):
        return None if self.level is None else N(self.level)

    def getName(self):
        return self.nm

    def getDuration(self):
        return self.dur

    def getStart(self):
        return None if self.start is None else Day(self.start)

    def getFinish(self):
        return None if self.finish is None else Day(self.finish)

    def getPercentageComplete(self):
        return None if self.pct is None else N(self.pct)

    def getPredecessors(self):
        return self.preds

    def getResourceAssignments(self):
        return self.res

    def getSummary(self):
        return self.summary

    def getMilestone(self):
        return self.milestone

    def getNotes(self):
        return self.notes

    def getNull(self):
        return self.null

    def getActive(self):
        return self.active


class Props:
    def __init__(self, title=None, start=None, finish=None, status=None):
        self.t, self.s, self.f, self.st = title, start, finish, status

    def getProjectTitle(self):
        return self.t

    def getStartDate(self):
        return None if self.s is None else Day(self.s)

    def getFinishDate(self):
        return None if self.f is None else Day(self.f)

    def getStatusDate(self):
        return None if self.st is None else Day(self.st)


class Project:
    def __init__(self, props, tasks=(), resources=()):
        self.p, self.tasks, self.res = props, tasks, resources

    def getProjectProperties(self):
        return self.p

    def getTasks(self):
        return iter(self.tasks)

    def getResources(self):
        return iter(self.res)


def render(doc, ctx, hidden=False):
    with tempfile.TemporaryDirectory() as d:
        return markdown.render(doc, Path(d) / "assets", hidden, ctx.skipped)


def sample_project():
    ann, bob = Res(1, "Ann"), Res(2, "Bob", "MATERIAL", 300.0)
    t0 = Task(0, "Launch", level=0, summary=True, dur=Dur(20.0), start="2026-01-05", finish="2026-03-27", pct=25.0)
    t1 = Task(1, "Design", wbs="1.1", dur=Dur(5.0), start="2026-01-05", finish="2026-01-09", pct=100.0,
              res=[Asg(ann, 50.0), Asg(bob), Asg(None)], notes="Check the\nbrief")
    t2 = Task(2, "Build", wbs="1.2", dur=Dur(2.5, "HOURS"), start="2026-01-12", finish="2026-01-12",
              preds=[Rel(t1, "FINISH_START", Dur(2.0)), Rel(t0, "START_START", Dur(0.0)),
                     Rel(t1, "FINISH_FINISH", Dur(-1.0)), Rel(None)])
    t3 = Task(3, "Go live", wbs="1.3", dur=Dur(0.0), milestone=True, start="2026-03-27", finish="2026-03-27")
    gone = Task(None, None, null=True, notes="never shown")
    off = Task(4, "Parked", wbs="1.4", active=False, notes="old idea")
    return Project(Props("Launch plan", "2026-01-05", "2026-03-27", "2026-02-01"), [t0, t1, gone, t2, t3, off],
                   [Res(0, None), ann, bob, Res(5, "Ghost", null=True), Res(6, "Idle", active=False)])


def test_build_output():
    ctx = Context()
    doc = mpp_mpxj.build(sample_project(), ctx)
    assert doc.title == "Launch plan"
    assert doc.blocks[0] == Para([Text("Start 2026-01-05 · Finish 2026-03-27 · Status date 2026-02-01")])
    heads = [b.inlines[0].text for b in doc.blocks if isinstance(b, Heading)]
    assert heads == ["Tasks", "Resources", "Notes"]
    md = render(doc, ctx)
    lines = md.splitlines()
    assert HEADER in lines, md
    assert "|0|0|**Launch**|20d|2026-01-05|2026-03-27|25%|||" in lines, md
    assert "|1|1.1|Design|5d|2026-01-05|2026-01-09|100%||Ann[50%], Bob|" in lines, md
    assert "|2|1.2|Build|2.5h|2026-01-12|2026-01-12||1FS+2d, 0SS, 1FF-1d||" in lines, md
    assert "|3|1.3|Go live (milestone)|0d|2026-03-27|2026-03-27||||" in lines, md
    assert "Parked" not in md and "never shown" not in md and "old idea" not in md  # inactive: hidden rows
    assert "|ID|Name|Type|Max units|" in lines
    assert "|1|Ann|Work|100%|" in lines and "|2|Bob|Material|300%|" in lines, md
    assert "Ghost" not in md and "Idle" not in md and "|0||Work|100%|" not in lines  # MPXJ's unnamed resource 0
    assert "- 1 Design: Check the" in md and "brief" in md, md
    shown = render(doc, Context(), hidden=True)
    assert "Parked" in shown and "4 Parked: old idea" in shown and "Idle" in shown
    assert not ctx.skipped.get("text") and not ctx.skipped.get("record") and not ctx.skipped.get("sheet_cut")
    # the Tasks table is one Table whose header is row 0
    tables = [b for b in doc.blocks if isinstance(b, Table)]
    assert len(tables) == 2 and tables[0].hidden_rows == frozenset({5}), tables[0].hidden_rows
    # bold summary name, milestone suffix
    name0 = tables[0].rows[1][2].blocks[0].inlines
    assert isinstance(name0[0], Bold) and name0[0].inlines == [Text("Launch")]
    name3 = tables[0].rows[4][2].blocks[0].inlines
    assert name3[-1] == Text(" (milestone)")


def test_build_partial_line_and_title():
    ctx = Context()
    doc = mpp_mpxj.build(Project(Props(None, "2026-01-05", None, "2026-02-01")), ctx)
    assert doc.title == "" and doc.blocks[0] == Para([Text("Start 2026-01-05 · Status date 2026-02-01")])
    doc = mpp_mpxj.build(Project(Props("T", None, None, None)), ctx)
    assert not any(isinstance(b, Para) for b in doc.blocks)
    md = render(doc, ctx)
    assert HEADER in md.splitlines() and "## Resources" not in md and "## Notes" not in md
    # an outline level stands in for a missing WBS; a summary milestone keeps both marks
    t = Task(7, "Gate", wbs="", level=3, summary=True, milestone=True)
    doc = mpp_mpxj.build(Project(Props(), [t]), Context())
    row = [b for b in doc.blocks if isinstance(b, Table)][0].rows[1]
    assert row[1].blocks[0].inlines == [Text("3")]
    assert row[2].blocks[0].inlines == [Bold([Text("Gate")]), Text(" (milestone)")]


def test_note_cut_counted_once():
    real = mpp_mpxj.MPP_NOTE
    mpp_mpxj.MPP_NOTE = 10  # read at call time
    try:
        ctx = Context()
        tasks = [Task(1, "A", notes="0123456789"), Task(2, "B", notes="0123456789X"), Task(3, "C", notes="x" * 50)]
        doc = mpp_mpxj.build(Project(Props(), tasks), ctx)
        items = [b for b in doc.blocks if isinstance(b, ListItem)]
        text = ["".join(i.text for i in it.inlines if isinstance(i, Text)) for it in items]
        assert text == ["1 A: 0123456789", "2 B: 0123456789…", "3 C: xxxxxxxxxx…"], text
        assert ctx.skipped["text"] == 2, ctx.skipped
    finally:
        mpp_mpxj.MPP_NOTE = real


def test_notes_share_the_character_budget():
    # every note is charged against the document's sheet characters; past them the notes stop, counted once
    real = limits.SHEET_CHARS
    limits.SHEET_CHARS = 400
    try:
        ctx = Context()
        tasks = [Task(i, "T", notes="n" * 60) for i in range(1, 20)]
        doc = mpp_mpxj.build(Project(Props(), tasks), ctx)
        items = [b for b in doc.blocks if isinstance(b, ListItem)]
        # 400 - 63 (the header) - 19 rows of 3 = 280, charged after the grid: four notes of 65 fit, the fifth not
        assert len(items) == 4, len(items)
        assert ctx.skipped["sheet_cut"] == 1 and "notes cut at 400 characters" in ctx.warnings, (ctx.skipped,
                                                                                                ctx.warnings)
        assert sum(ctx.skipped.values()) == 1
    finally:
        limits.SHEET_CHARS = real


def test_notes_after_both_grids():
    # notes are charged only after the Tasks and Resources grids: they never starve a task row or a resource
    real = limits.SHEET_CHARS
    limits.SHEET_CHARS = 300  # header 63 + 5 rows of 3 + resources 19 + 2 rows of 12 = 121; one note of 105 fits
    try:
        ctx = Context()
        tasks = [Task(i, "T", notes="n" * 100) for i in range(1, 6)]
        doc = mpp_mpxj.build(Project(Props(), tasks, [Res(1, "Ann"), Res(2, "Bob")]), ctx)
        md = render(doc, ctx)
        assert all(f"|{i}|1|T|" in md for i in range(1, 6)), md
        assert "|1|Ann|Work|100%|" in md and "|2|Bob|Work|100%|" in md, md
        items = [b for b in doc.blocks if isinstance(b, ListItem)]
        assert len(items) == 1 and ctx.skipped["sheet_cut"] == 1, (len(items), ctx.skipped)
        heads = [b.inlines[0].text for b in doc.blocks if isinstance(b, Heading)]
        assert heads == ["Tasks", "Resources", "Notes"]
    finally:
        limits.SHEET_CHARS = real
    # once the notes are closed, no later task's note is read
    reads = []

    class Noted(Task):
        def getNotes(self):
            reads.append(self.tid)
            return "n" * 100

    limits.SHEET_CHARS = 300
    try:
        mpp_mpxj.build(Project(Props(), [Noted(i, "T") for i in range(1, 6)]), Context())
        assert reads == [1, 2, 3], reads  # 63 + 15 + 105 + 105 = 288; the third does not fit and closes the notes
    finally:
        limits.SHEET_CHARS = real


def test_rows_and_cells_capped():
    real = limits.SHEET_ROWS
    limits.SHEET_ROWS = 4  # header + 3 tasks
    try:
        ctx = Context()
        tasks = [Task(i, f"T{i}", notes=f"note {i}") for i in range(1, 50)]
        doc = mpp_mpxj.build(Project(Props(), tasks, [Res(i, f"R{i}") for i in range(1, 50)]), ctx)
        md = render(doc, ctx)
        assert "|3|1|T3|" in md and "T4" not in md, md
        assert "note 3" in md and "note 4" not in md  # a task past the cut gives no note
        assert ctx.skipped["sheet_cut"] == 2, ctx.skipped  # Tasks once, Resources once
    finally:
        limits.SHEET_ROWS = real
    real = limits.SHEET_CELLS
    limits.SHEET_CELLS = 9 + 9 + 4  # the header, one task and part of a second
    try:
        ctx = Context()
        doc = mpp_mpxj.build(Project(Props(), [Task(i, f"T{i}", dur=Dur(1.0), start="2026-01-01", finish="2026-01-02",
                                                    pct=0.0, wbs=str(i), notes=f"note {i}") for i in range(1, 50)],
                                     [Res(1, "R1")]), ctx)
        assert ctx.skipped["sheet_cut"] == 2, ctx.skipped  # the tasks grid, then the resources grid finds no cell
        md = render(doc, ctx)
        assert "T1" in md and "T3" not in md
        assert "note 1" in md and "note 2" not in md, md  # the row cut part way gives no note
        limits.SHEET_CELLS = 3  # not even the header: no task is read
        read_ = []

        class Spy(Task):
            def getNull(self):
                read_.append(1)
                return False

        ctx = Context()
        mpp_mpxj.build(Project(Props(), [Spy(1, "A"), Spy(2, "B")]), ctx)
        assert read_ == [] and ctx.skipped["sheet_cut"] == 1, (read_, ctx.skipped)
    finally:
        limits.SHEET_CELLS = real


def test_rows_capped_without_values():
    # tasks that put no value still take a row: the row cap ends the loop, counted once
    real = limits.SHEET_ROWS
    limits.SHEET_ROWS = 5
    try:
        ctx = Context()
        seen = []

        class Empty(Task):
            def getNull(self):
                seen.append(1)
                return False

        mpp_mpxj.build(Project(Props(), [Empty(None, None, level=None) for _ in range(1000)]), ctx)
        # the header is row 0: SHEET_ROWS - 1 tasks are visited, the next one is not
        assert len(seen) == 4 and ctx.skipped["sheet_cut"] == 1, (len(seen), ctx.skipped)
        seen.clear()
        ctx = Context()
        mpp_mpxj.build(Project(Props(), [Empty(None, None, level=None) for _ in range(4)]), ctx)
        assert len(seen) == 4 and not ctx.skipped, (len(seen), ctx.skipped)  # exactly SHEET_ROWS - 1: no cut
        seen.clear()

        class NullRes(Res):
            def getNull(self):
                seen.append(1)
                return True

        ctx = Context()
        doc = mpp_mpxj.build(Project(Props(), [], [NullRes(i, "R") for i in range(1000)]), ctx)
        assert len(seen) == 4 and ctx.skipped["sheet_cut"] == 1, (len(seen), ctx.skipped)
        assert not any(isinstance(b, Heading) and b.inlines[0].text == "Resources" for b in doc.blocks)
        seen.clear()
        ctx = Context()
        mpp_mpxj.build(Project(Props(), [], [NullRes(i, "R") for i in range(4)]), ctx)
        assert len(seen) == 4 and not ctx.skipped, (len(seen), ctx.skipped)
    finally:
        limits.SHEET_ROWS = real


def test_join_bounded():
    # a predecessor or resource list stops being read once its text passes the characters left
    real = limits.SHEET_CHARS
    limits.SHEET_CHARS = 200
    try:
        ctx = Context()
        reads = []

        class Many(list):
            def __bool__(self):  # a Java list of a million relations is not empty
                return True

            def __iter__(self):
                for i in range(1_000_000):
                    reads.append(i)
                    yield Rel(Task(i, "x"))

        t = Task(1, "Big", preds=Many())
        mpp_mpxj.build(Project(Props(), [t]), ctx)
        assert len(reads) < 100, len(reads)
        assert ctx.skipped["sheet_cut"] == 1
        reads.clear()

        class ManyRes(list):
            def __bool__(self):
                return True

            def __iter__(self):
                for i in range(1_000_000):
                    reads.append(i)
                    yield Asg(Res(i, "Resource name"))

        ctx = Context()
        mpp_mpxj.build(Project(Props(), [Task(1, "Big", res=ManyRes())]), ctx)
        assert len(reads) < 100, len(reads)
        reads.clear()

        class Unnamed(list):  # relations whose predecessor has no ID give no text, yet each is charged
            def __bool__(self):
                return True

            def __iter__(self):
                for i in range(1_000_000):
                    reads.append(i)
                    yield Rel(None)

        ctx = Context()
        mpp_mpxj.build(Project(Props(), [Task(1, "Big", preds=Unnamed())]), ctx)
        assert len(reads) < 1000, len(reads)
    finally:
        limits.SHEET_CHARS = real


def test_task_guarded():
    # a task whose getters fail is skipped and counted under record; the rest are read
    ctx = Context()
    doc = mpp_mpxj.build(Project(Props(), [Task(1, "A"), Task(2, "B", broken=True, notes="lost"), Task(3, "C")]), ctx)
    md = render(doc, ctx)
    assert "|1|1|A|" in md and "|2|1|" not in md and "|3|1|C|" in md and "lost" not in md, md
    assert ctx.skipped["record"] == 1

    class Broken(Res):
        def getName(self):
            raise RuntimeError("a getter MPXJ could not answer")

    ctx = Context()
    doc = mpp_mpxj.build(Project(Props(), [], [Res(1, "Ann"), Broken(2, "x"), Res(3, "Cy")]), ctx)
    md = render(doc, ctx)
    assert "|1|Ann|Work|100%|" in md and "|3|Cy|Work|100%|" in md and ctx.skipped["record"] == 1, md


# ---- read(): the JVM, MPPReader and the exceptions, against stand-in jpype and mpxj modules (no Java here)

class FakeJava:
    """sys.modules["jpype"] and ["mpxj"]: records startJVM and answers JClass with stand-in classes."""

    def __init__(self, project=None, raise_with=None, other=None):
        self.started, self.calls, self.out = False, [], []
        java = self

        class JException(Exception):
            pass

        class MPXJException(JException):
            PASSWORD_PROTECTED = "File is password protected"

            def getMessage(self):
                return self.args[0] if self.args else None

        class MPPReader:
            def read(self, f):
                java.calls.append(("read", f))
                if raise_with is not None:
                    raise MPXJException(raise_with)
                if other is not None:
                    raise other
                return project

        class File:
            def __init__(self, p):
                self.p = p

        class System:
            err = "stderr stream"

            @staticmethod
            def setOut(s):
                java.out.append(s)

        self.classes = {"org.mpxj.mpp.MPPReader": MPPReader, "org.mpxj.MPXJException": MPXJException,
                        "java.io.File": File, "java.lang.System": System}
        jp = types.ModuleType("jpype")
        jp.JException = JException
        jp.isJVMStarted = lambda: java.started
        jp.JClass = lambda name: java.classes[name]

        def startJVM(*args, **kw):
            java.calls.append(("startJVM", args, kw))
            java.started = True
        jp.startJVM = startJVM
        self.jpype, self.mpxj = jp, types.ModuleType("mpxj")
        self.JException = JException

    def __enter__(self):
        self.saved = {k: sys.modules.get(k) for k in ("jpype", "mpxj")}
        sys.modules["jpype"], sys.modules["mpxj"] = self.jpype, self.mpxj
        self.prefix = sys.prefix
        self.tmp = tempfile.TemporaryDirectory()
        sys.prefix = self.tmp.name
        (Path(self.tmp.name) / "ready.json").write_text(json.dumps(
            {"kind": "mpp", "java": {"jvm": "C:/jdk/bin/server/jvm.dll", "major": 21}}), encoding="utf-8")
        return self

    def __exit__(self, *exc):
        sys.prefix = self.prefix
        self.tmp.cleanup()
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v


def test_read_starts_jvm_and_reads():
    with FakeJava(project=sample_project()) as j, tempfile.TemporaryDirectory() as d:
        p = project_file(d)
        doc = mpp_mpxj.read(p, Context())
        assert doc.title == "Launch plan"
        start = [c for c in j.calls if c[0] == "startJVM"]
        assert len(start) == 1
        _, args, kw = start[0]
        assert "-Xmx1024m" in args and "-Djava.awt.headless=true" in args
        assert kw.get("jvmpath") == "C:/jdk/bin/server/jvm.dll" and kw.get("convertStrings") is False
        reads = [c for c in j.calls if c[0] == "read"]
        assert len(reads) == 1 and Path(reads[0][1].p) == p.resolve()
        assert j.out == ["stderr stream"]  # Java's System.out goes to stderr, off the protocol stream
        mpp_mpxj.read(p, Context())  # a started JVM is not started again
        assert len([c for c in j.calls if c[0] == "startJVM"]) == 1


def test_read_password_encrypted():
    for msg in ("File is password protected", "File is password protected. Please enter password."):
        with FakeJava(raise_with=msg), tempfile.TemporaryDirectory() as d:
            e = raises(ReadError, mpp_mpxj.read, project_file(d), Context())
            assert e.kind == "encrypted" and e.detail == "Project password set", e


def test_read_other_errors_damaged():
    with FakeJava(raise_with="Invalid file format: MSProject.MPP4"), tempfile.TemporaryDirectory() as d:
        e = raises(ReadError, mpp_mpxj.read, project_file(d), Context())
        assert e.kind == "damaged" and "Invalid file format" not in e.detail
    with FakeJava() as j, tempfile.TemporaryDirectory() as d:
        class Crash:
            def read(self, f):
                raise j.JException("java.lang.OutOfMemoryError")  # any Java throwable other than MPXJException

        j.classes["org.mpxj.mpp.MPPReader"] = Crash
        e = raises(ReadError, mpp_mpxj.read, project_file(d), Context())
        assert e.kind == "damaged"

    class Bad(Project):
        def getTasks(self):
            raise ValueError("a Java exception while reading tasks")

    with FakeJava(project=Bad(Props())), tempfile.TemporaryDirectory() as d:
        e = raises(ReadError, mpp_mpxj.read, project_file(d), Context())
        assert e.kind == "damaged"


def test_read_without_ready_json():
    with FakeJava(project=sample_project()) as j, tempfile.TemporaryDirectory() as d:
        (Path(sys.prefix) / "ready.json").unlink()
        e = raises(ReadError, mpp_mpxj.read, project_file(d), Context())
        assert e.kind == "unsupported" and e.detail == UNSUPPORTED and not j.calls
        for bad in ('{"kind": "mpp", "java": {"jvm": 7}}', '{"kind": "mpp", "java": {"jvm": ""}}',
                    '{"java": {"jvm": "C:/jdk/bin/server/jvm.dll"}}', '{"kind": "pdf", "java": {"jvm": "C:/j.dll"}}',
                    '{"kind": "mpp", "java": "C:/j.dll"}', '[1]', 'not json'):
            (Path(sys.prefix) / "ready.json").write_text(bad, encoding="utf-8")
            e = raises(ReadError, mpp_mpxj.read, project_file(d), Context())
            assert e.kind == "unsupported" and not j.calls, bad


# ---- venv only: MPXJ's own test files, read under the mpp venv's interpreter

def _venv_samples():
    """(interpreter, samples folder), or None after printing why the venv tests skip."""
    venv = pyenv.venv_dir("mpp")
    if pyenv.state(venv, "mpp") != "READY":
        print("skip: no ready mpp venv")
        return None
    folder = os.environ.get("PHRO_READER_MPP_SAMPLES")
    if not folder or not Path(folder).is_dir():
        print("skip: PHRO_READER_MPP_SAMPLES not set")
        return None
    return pyenv.interpreter(venv), Path(folder)


def _child(py, src, out):
    r = subprocess.run([str(py), "-I", "-X", "utf8", str(READ_PY), "convert", str(src), "--engine", "mpxj",
                        "--out", str(out)], capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=limits.CONVERT_SECONDS + 60)
    return r.returncode, r.stdout.strip()


def test_mpxj_samples():
    got = _venv_samples()
    if got is None:
        return
    py, folder = got
    import re
    files = sorted(f for f in folder.glob("*.mpp") if "password" not in f.name.lower())
    assert files, "no .mpp files in PHRO_READER_MPP_SAMPLES"
    with_resources = 0
    for i, f in enumerate(files):
        with tempfile.TemporaryDirectory() as d:
            code, out = _child(py, f, Path(d) / "o")
            assert code == 0, (i, out)
            md = (Path(d) / "o" / "out.md").read_text(encoding="utf-8").splitlines()
            assert "## Tasks" in md and HEADER in md, i
            at = md.index(HEADER)
            assert len(md) > at + 2 and md[at + 2].startswith("|"), i  # the header, its rule, then a row
            # a getter MPXJ lacks makes a task or resource a damaged record: none may be
            unread = json.loads((Path(d) / "o" / "meta.json").read_text(encoding="utf-8"))["unread"]
            assert not any("damaged record" in u for u in unread), (i, unread)
            # a task row naming a resource (through _assignments) means the Resources table (through _resource)
            rows = []
            for line in md[at + 2:]:
                if not line.startswith("|"):
                    break
                rows.append(re.split(r"(?<!\\)\|", line))
            named = any(len(c) > 9 and c[9].strip() for c in rows)
            if "## Resources" in md:
                with_resources += 1
            assert not named or "## Resources" in md, i
    assert with_resources, "no sample showed a Resources table"


def test_mpxj_password_encrypted():
    got = _venv_samples()
    if got is None:
        return
    py, folder = got

    def find(name):
        return next(iter(sorted(folder.rglob(name))), None)

    for name in ("readpassword9.mpp", "password-protected-mpp9.mpp"):
        f = find(name)
        if f is None:
            print(f"skip: {name} not under PHRO_READER_MPP_SAMPLES")
            continue
        with tempfile.TemporaryDirectory() as d:
            code, out = _child(py, f, Path(d) / "o")
            assert code == 1 and out == "FAIL\tencrypted\tProject password set", (name, out)
    f = find("writepassword9.mpp")  # the protection flag without a read password: read (Open point 6)
    if f is None:
        print("skip: writepassword9.mpp not under PHRO_READER_MPP_SAMPLES")
        return
    with tempfile.TemporaryDirectory() as d:
        code, out = _child(py, f, Path(d) / "o")
        assert code == 0 and (Path(d) / "o" / "out.md").is_file(), out


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
