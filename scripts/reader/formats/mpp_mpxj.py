"""Microsoft Project through MPXJ, in the mpp venv's child only (`read.py convert <path> --engine mpxj`).

MPXJ (https://www.mpxj.org, LGPL-2.1) is installed into the plugin-managed mpp venv on the user's yes and called
through JPype; none of its code is copied here, and no mpp structure is parsed in this module. `jpype` and `mpxj` are
imported inside `read` only, so the gate and the test suite import this module without them.

`read` starts the JVM once, with the JVM library recorded in the venv's ready.json, `-Xmx1024m` (what MPXJ may
materialize) and headless, moves Java's System.out to stderr (the child's stdout carries the protocol line), and reads
with `org.mpxj.mpp.MPPReader`, never `UniversalProjectReader`, so a renamed file of another format is not read through
MPXJ (mpp.parse has already sent such a file to cfb_route). An MPXJException whose message starts with
`MPXJException.PASSWORD_PROTECTED`, read from the class (MPP9's read password raises
PASSWORD_PROTECTED_ENTER_PASSWORD, which starts with it; MPP12 and MPP14 raise it as is), is `encrypted`; any other
failure, MPXJ's or the JVM's, is `damaged` with a fixed detail, since MPXJ's messages may quote the file.

`build` turns MPXJ's project into the Doc (spec "Project", Output): the project title, one `Start · Finish · Status
date` line (the parts that are set), `## Tasks` and `## Resources` through `sheet_grid.Grid` (one `Budget` a document,
so SHEET_ROWS, SHEET_CELLS and SHEET_CHARS bound them; every task or resource visited counts toward SHEET_ROWS, null
ones too), then `## Notes`, a ListItem per task note cut at MPP_NOTE characters (counted `text`) and charged against
the same Budget's characters (past them the notes stop, counted `sheet_cut` once). Null tasks are skipped, inactive
tasks and resources are hidden rows (their notes hidden items), a summary task's name is bold and a milestone's ends
in ` (milestone)`. A predecessor or resource list is read only until its text passes the characters left. Each task
and resource is read under `ctx.guard("record")` and put only once read whole. Calendars, views, filters, baselines
and time-phased data are not read.
"""
import json
import math
import re
import sys
from pathlib import Path

from .. import limits
from ..limits import ReadError
from ..model import Bold, Doc, Heading, ListItem, Para, Text
from . import sheet_grid
from .mpp import UNSUPPORTED

MPP_NOTE = 20_000  # characters of one task note; past it the note is cut with "…", counted text
JVM_ARGS = ("-Xmx1024m", "-Djava.awt.headless=true")
PASSWORD = "Project password set"
DAMAGED = "MPXJ could not read the file"
NO_JAVA = "the Java VM did not start"
TASK_COLUMNS = ("ID", "Outline", "Name", "Duration", "Start", "Finish", "% Complete", "Predecessors", "Resources")
RESOURCE_COLUMNS = ("ID", "Name", "Type", "Max units")
# org.mpxj.TimeUnit constant -> its short form (plan decision 9)
UNITS = {"MINUTES": "m", "HOURS": "h", "DAYS": "d", "WEEKS": "w", "MONTHS": "mo", "YEARS": "y", "PERCENT": "%",
         "ELAPSED_MINUTES": "em", "ELAPSED_HOURS": "eh", "ELAPSED_DAYS": "ed", "ELAPSED_WEEKS": "ew",
         "ELAPSED_MONTHS": "emo", "ELAPSED_YEARS": "ey", "ELAPSED_PERCENT": "e%"}
# org.mpxj.RelationType constant -> its short form
RELATIONS = {"FINISH_START": "FS", "START_START": "SS", "FINISH_FINISH": "FF", "START_FINISH": "SF"}
_ENUM = re.compile(r"[A-Z][A-Z_]{0,31}")  # an unknown unit shown by name only when it looks like a Java constant
SEP = ", "
DOT = " · "


# ---- pure helpers

def _num(x) -> str:
    """A finite number with no trailing ".0" (15 significant digits, so a huge one stays short); "" otherwise."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return ""
    try:
        x = float(x)
    except OverflowError:
        return ""
    if not math.isfinite(x):
        return ""
    return format(x + 0.0 if x else 0.0, ".15g")


def duration_text(amount: float, unit: str) -> str:
    """`5d`, `2.5h`, `3ed`: the amount, then the unit's short form; an unknown unit by its name in lower case after a
    space, or left out when it is not a plain constant name. "" when the amount is not a finite number."""
    s = _num(amount)
    if not s:
        return ""
    if isinstance(unit, str):
        if unit in UNITS:
            return s + UNITS[unit]
        if _ENUM.fullmatch(unit):
            return f"{s} {unit.lower()}"
    return s


def relation_text(task_id: int, kind: str, lag_amount: float, lag_unit: str) -> str:
    """`3FS+2d`, `4SS`, `3FS-1d`: the predecessor's ID, the relation's short form (left out when unknown) and a lag
    other than 0 with its sign (a lag that is not a finite number is left out)."""
    out = f"{task_id}{RELATIONS.get(kind, '') if isinstance(kind, str) else ''}"
    if _num(lag_amount) and lag_amount != 0:
        out += ("+" if lag_amount > 0 else "-") + duration_text(abs(float(lag_amount)), lag_unit)
    return out


def resource_text(name: str, units: float) -> str:
    """`Ann` at 100 % (or when the units are not a finite number), else `Ann[50%]`."""
    name = "" if name is None else str(name)
    u = _num(units)
    if not u or float(units) == 100:
        return name
    return f"{name}[{u}%]"


def refusal(message: str, password_prefix: str) -> ReadError:
    """The ReadError for an MPXJException with `message`: encrypted when it starts with MPXJ's PASSWORD_PROTECTED
    text (`password_prefix`, read from the class), else damaged with a fixed detail."""
    if password_prefix and isinstance(message, str) and message.startswith(password_prefix):
        return ReadError("encrypted", PASSWORD)
    return ReadError("damaged", DAMAGED)


# ---- MPXJ's values (Java objects through JPype; the tests answer the same calls in Python)

def _s(x) -> str:
    return "" if x is None else str(x).replace("\r\n", "\n").replace("\r", "\n")


def _float(x):
    """A java.lang.Number (or a Python number) as a float, or None."""
    if x is None:
        return None
    try:
        return float(x.doubleValue()) if hasattr(x, "doubleValue") else float(x)
    except (TypeError, ValueError, OverflowError):
        return None


def _int(x):
    if x is None:
        return None
    try:
        return int(x.intValue()) if hasattr(x, "intValue") else int(x)
    except (TypeError, ValueError, OverflowError):
        return None


def _const(e) -> str:
    """A Java enum constant's name, "" for null."""
    return "" if e is None else str(e.name())


def _label(e) -> str:
    """WORK -> Work, NON_LABOR -> Non labor."""
    return _const(e).replace("_", " ").capitalize()


def _day(dt) -> str:
    """A LocalDateTime as YYYY-MM-DD, "" for null."""
    return "" if dt is None else str(dt.toLocalDate())


def _duration(d) -> str:
    return "" if d is None else duration_text(_float(d.getDuration()), _const(d.getUnits()))


def _joined(texts, room: int) -> str:
    """The non-empty texts joined with ", ", read only until the joined text passes `room` characters (an empty one
    is charged a character, so a list of empties stops too)."""
    out, n = [], 0
    for t in texts:
        if t:
            n += len(t) + (len(SEP) if out else 0)
            out.append(t)
        else:
            n += 1
        if n > room:
            break
    return SEP.join(out)


def _predecessors(task):
    for rel in task.getPredecessors() or ():
        other = rel.getPredecessorTask()
        tid = None if other is None else _int(other.getID())
        if tid is None:
            yield ""
            continue
        lag = rel.getLag()
        yield relation_text(tid, _const(rel.getType()), None if lag is None else _float(lag.getDuration()),
                            "" if lag is None else _const(lag.getUnits()))


def _assignments(task):
    for a in task.getResourceAssignments() or ():
        res = a.getResource()
        yield "" if res is None else resource_text(_s(res.getName()), _float(a.getUnits()))


def _name_cell(name: str, summary: bool, milestone: bool) -> list:
    inl = sheet_grid.text_inlines(name) if name else []
    if summary and inl:
        inl = [Bold(inl)]
    if milestone:
        inl.append(Text(" (milestone)" if inl else "(milestone)"))
    return inl


def _task(t, room: int):
    """(row values, active, ID, name, note) of a task that is not null, else None."""
    if t.getNull():
        return None
    tid, name = _int(t.getID()), _s(t.getName())
    wbs, level = _s(t.getWBS()), _int(t.getOutlineLevel())
    pct = _num(_float(t.getPercentageComplete()))
    ident = "" if tid is None else str(tid)
    row = [ident, wbs or ("" if level is None else str(level)),
           _name_cell(name, bool(t.getSummary()), bool(t.getMilestone())), _duration(t.getDuration()),
           _day(t.getStart()), _day(t.getFinish()), pct + "%" if pct else "",
           _joined(_predecessors(t), room), _joined(_assignments(t), room)]
    return row, bool(t.getActive()), ident, " ".join(name.split()), _s(t.getNotes())


def _put(grid, r: int, row: list) -> bool:
    for c, v in enumerate(row):
        if not grid.put(r, c, v):
            return False
    return True


class _Notes:
    """The Notes list: each note cut at MPP_NOTE (counted text) and charged against the Budget's characters; once
    they are spent the notes stop, counted sheet_cut once."""

    def __init__(self, ctx, budget):
        self.ctx, self.budget, self.items, self.open = ctx, budget, [], True

    def add(self, ident: str, name: str, note: str, active: bool):
        if not self.open or not note:
            return
        cut = len(note) > MPP_NOTE
        if cut:
            note = note[:MPP_NOTE] + "…"
        head = f"{ident} {name}: " if name else f"{ident}: "
        n = len(head) + len(note)
        if self.budget.chars < n:
            self.open = False
            self.ctx.warnings.append(f"notes cut at {self.budget.cap_chars} characters")
            self.ctx.skipped["sheet_cut"] += 1
            return
        self.budget.chars -= n
        if cut:
            self.ctx.skipped["text"] += 1
        self.items.append(ListItem(0, "-", [Text(head)] + sheet_grid.text_inlines(note),
                                   hidden="" if active else "hidden"))


def _tasks(project, ctx, budget, notes) -> list:
    grid = sheet_grid.Grid("Tasks", budget, ctx)
    _put(grid, 0, list(TASK_COLUMNS))
    r = seen = 1
    for t in project.getTasks():
        if grid.cut:  # the header did not fit: no task is read
            break
        if seen >= limits.SHEET_ROWS:
            grid.stop("rows", limits.SHEET_ROWS)
            break
        seen += 1
        got = None
        with ctx.guard("record"):
            got = _task(t, budget.chars)
        if got is None:
            continue
        row, active, ident, name, note = got
        if not active:
            grid.hide_row(r)
        if not _put(grid, r, row):
            break
        r += 1
        notes.add(ident, name, note, active)
    return grid.blocks()


def _resource(res):
    """(row values, active) of a resource that is not null and has a name or an ID other than 0, else None."""
    if res.getNull():
        return None
    rid, name = _int(res.getID()), _s(res.getName())
    if not name and not rid:  # MPXJ's unnamed resource 0, and blank resource rows
        return None
    mu = _num(_float(res.getMaxUnits()))
    row = ["" if rid is None else str(rid), name, _label(res.getType()), mu + "%" if mu else ""]
    return row, bool(res.getActive())


def _resources(project, ctx, budget) -> list:
    """The Resources table, or [] when no resource row was put (its header is put with the first row)."""
    grid = sheet_grid.Grid("Resources", budget, ctx)
    r = seen = 1
    for res in project.getResources():
        if seen >= limits.SHEET_ROWS:
            grid.stop("rows", limits.SHEET_ROWS)
            break
        seen += 1
        got = None
        with ctx.guard("record"):
            got = _resource(res)
        if got is None:
            continue
        row, active = got
        if r == 1 and not _put(grid, 0, list(RESOURCE_COLUMNS)):
            break
        if not active:
            grid.hide_row(r)
        if not _put(grid, r, row):
            break
        r += 1
    return grid.blocks()  # no cells (no resource, or none put): []


def build(project, ctx) -> Doc:
    """The Doc of an MPXJ ProjectFile (or anything answering the same calls)."""
    props = project.getProjectProperties()
    title = " ".join(_s(props.getProjectTitle()).split())
    parts = [f"{label} {day}" for label, day in (("Start", _day(props.getStartDate())),
                                                 ("Finish", _day(props.getFinishDate())),
                                                 ("Status date", _day(props.getStatusDate()))) if day]
    blocks = [Para([Text(DOT.join(parts))])] if parts else []
    budget = sheet_grid.Budget(limits.SHEET_CELLS, limits.SHEET_CHARS)
    notes = _Notes(ctx, budget)
    blocks += [Heading(2, [Text("Tasks")])] + _tasks(project, ctx, budget, notes)
    res = _resources(project, ctx, budget)
    if res:
        blocks += [Heading(2, [Text("Resources")])] + res
    if notes.items:
        blocks += [Heading(2, [Text("Notes")])] + notes.items
    return Doc(title=title, blocks=blocks)


# ---- the JVM

def _recorded_jvm() -> str:
    """The JVM library the mpp venv's setup recorded (ready.json in sys.prefix, the venv); `unsupported` when this
    interpreter is not a ready mpp venv's."""
    try:
        ready = json.loads((Path(sys.prefix) / "ready.json").read_text(encoding="utf-8"))
        jvm = ready["java"]["jvm"] if ready.get("kind") == "mpp" else None
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        jvm = None
    if not isinstance(jvm, str) or not jvm:
        raise ReadError("unsupported", UNSUPPORTED)
    return jvm


def read(path, ctx) -> Doc:
    """Read `path` with MPXJ's MPPReader in this process's JVM (started here once) and build its Doc."""
    jvm = _recorded_jvm()
    import jpype
    import mpxj  # noqa: F401 - MPXJ's package puts its jars on JPype's class path; it must come before startJVM
    if not jpype.isJVMStarted():
        try:
            jpype.startJVM(*JVM_ARGS, jvmpath=jvm, convertStrings=False)
            system = jpype.JClass("java.lang.System")
            system.setOut(system.err)  # nothing Java prints may reach the protocol stream
        except Exception:  # noqa: BLE001 - the library would not load, or the JVM refused its arguments
            raise ReadError("damaged", NO_JAVA) from None
    mpxj_exception = jpype.JClass("org.mpxj.MPXJException")
    try:
        reader = jpype.JClass("org.mpxj.mpp.MPPReader")()
        project = reader.read(jpype.JClass("java.io.File")(str(Path(path).resolve())))
    except mpxj_exception as e:
        raise refusal(_s(e.getMessage()), _s(mpxj_exception.PASSWORD_PROTECTED)) from None
    except Exception:  # noqa: BLE001 - any other Java throwable (JPype raises them as Python exceptions)
        raise ReadError("damaged", DAMAGED) from None
    try:
        return build(project, ctx)
    except ReadError:
        raise
    except Exception:  # noqa: BLE001 - a getter MPXJ could not answer outside a task or resource
        raise ReadError("damaged", DAMAGED) from None
