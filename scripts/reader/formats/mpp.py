"""Microsoft Project (mpp, mpt), read by `mpp_read` (a port of MPXJ's MPPReader) and built into the Doc here.

The compound file is opened once. One whose `\\x01CompObj` clipboard format or ProgID does not start `MSProject.` is
another format's compound file named .mpp, handed to `cfb_route` as ppt's parse_compound does. A zip named .mpp goes
to `zip_route`.

`build` (spec "Project", Output): the project title, one `Start · Finish · Status date` line (the parts that are set),
`## Tasks` and `## Resources` through `sheet_grid.Grid` (one `Budget` a document, so SHEET_ROWS, SHEET_CELLS and
SHEET_CHARS bound them; every task or resource visited counts toward SHEET_ROWS, null ones too), then `## Notes`, a
ListItem per task note cut at MPP_NOTE characters (counted `text`) and charged against the same Budget's characters
only after both grids, so a note never takes a task row's or a resource's characters (past them the notes stop,
counted `sheet_cut` once, and no later note is read). Null tasks are skipped, inactive tasks and resources are hidden
rows (their notes hidden items), a summary task's name is bold and a milestone's ends in ` (milestone)`. A
predecessor or resource list is read only until its text passes the characters left. Each task and resource is put
under `ctx.guard("record")` and only once read whole.
"""
import math
import re

from .. import limits
from ..containers.cfb import Compound, is_cfb
from ..limits import ReadError
from ..model import Bold, Doc, Heading, ListItem, Para, Text
from . import cfb_route, mpp_notes, mpp_read, sheet_grid, zip_route

NOT_PROJECT = "no MSProject CompObj"
MPP_NOTE = 20_000
TASK_COLUMNS = ("ID", "Outline", "Name", "Duration", "Start", "Finish", "% Complete", "Predecessors", "Resources")
RESOURCE_COLUMNS = ("ID", "Name", "Type", "Max units")
UNITS = {"MINUTES": "m", "HOURS": "h", "DAYS": "d", "WEEKS": "w", "MONTHS": "mo", "YEARS": "y", "PERCENT": "%",
         "ELAPSED_MINUTES": "em", "ELAPSED_HOURS": "eh", "ELAPSED_DAYS": "ed", "ELAPSED_WEEKS": "ew",
         "ELAPSED_MONTHS": "emo", "ELAPSED_YEARS": "ey", "ELAPSED_PERCENT": "e%"}
RELATIONS = {"FINISH_START": "FS", "START_START": "SS", "FINISH_FINISH": "FF", "START_FINISH": "SF"}
_ENUM = re.compile(r"[A-Z][A-Z_]{0,31}")
SEP = ", "
DOT = " · "


def parse(path, ctx) -> Doc:
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:2] == b"PK":
        return zip_route.parse(path, ctx)
    if not is_cfb(head):
        raise ReadError("damaged", "not a Microsoft Project file")
    with Compound(path) as cf:
        if cfb_route.msproject(cf):
            project = mpp_read.read(cf)
            other = None
        else:
            other = cfb_route.route(cf, "mpp", NOT_PROJECT)
    if other is not None:
        return cfb_route.parse(other, path, ctx)
    return build(project, ctx)


def _num(x) -> str:
    """A finite number with no trailing ".0" (15 significant digits, so a huge one stays short); "" otherwise."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return ""
    x = float(x)
    if not math.isfinite(x):
        return ""
    return format(x + 0.0 if x else 0.0, ".15g")


def duration_text(d) -> str:
    """`5d`, `2.5h`, `3ed` from (amount, unit): the amount, then the unit's short form; an unknown unit by its name in
    lower case after a space, or left out when it is not a plain constant name. A duration given as text is that
    text; "" for none."""
    if d is None:
        return ""
    if isinstance(d, str):
        return d
    amount, unit = d
    s = _num(amount)
    if not s:
        return ""
    if unit in UNITS:
        return s + UNITS[unit]
    if isinstance(unit, str) and _ENUM.fullmatch(unit):
        return f"{s} {unit.lower()}"
    return s


def relation_text(task_id: int, kind: str, lag) -> str:
    """`3FS+2d`, `4SS`, `3FS-1d`: the predecessor's ID, the relation's short form and a lag other than 0 with its
    sign."""
    out = f"{task_id}{RELATIONS.get(kind, '')}"
    if isinstance(lag, tuple) and _num(lag[0]) and lag[0] != 0:
        out += ("+" if lag[0] > 0 else "-") + duration_text((abs(float(lag[0])), lag[1]))
    return out


def resource_text(name, units) -> str:
    """`Ann` at 100 % (or when the units are not a finite number), else `Ann[50%]`."""
    name = name or ""
    u = _num(units)
    if not u or float(units) == 100:
        return name
    return f"{name}[{u}%]"


def _text(x) -> str:
    return "" if x is None else str(x).replace("\r\n", "\n").replace("\r", "\n")


def _day(dt) -> str:
    return "" if dt is None else dt.date().isoformat()


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


def _predecessors(project, t):
    for rel in project.predecessors(t):
        tid = rel.pred.id
        yield "" if tid is None else relation_text(tid, rel.kind, rel.lag)


def _assignments(project, t):
    for a in project.assignments(t):
        res = project.resource_uids.get(a.resource_uid)
        yield "" if res is None else resource_text(_text(res.f.get("name")), a.units)


def _name_cell(name: str, summary: bool, milestone: bool) -> list:
    inl = sheet_grid.text_inlines(name) if name else []
    if summary and inl:
        inl = [Bold(inl)]
    if milestone:
        inl.append(Text(" (milestone)" if inl else "(milestone)"))
    return inl


def _task(project, t, room: int):
    """(row values, active, ID, name) of a task that is not null, else None."""
    if t.null:
        return None
    f = t.f
    tid, name = t.id, _text(f.get("name"))
    wbs, level = _text(f.get("wbs")), f.get("outline_level")
    pct = _num(f.get("percent"))
    ident = "" if tid is None else str(tid)
    row = [ident, wbs or ("" if level is None else str(level)), _name_cell(name, t.summary, t.milestone),
           duration_text(f.get("duration")), _day(f.get("start")), _day(f.get("finish")), pct + "%" if pct else "",
           _joined(_predecessors(project, t), room), _joined(_assignments(project, t), room)]
    return row, t.active, ident, " ".join(name.split())


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

    def read(self, pending):
        """The notes of the tasks put, (task, ID, name, active) in order, read only while the notes are open."""
        for t, ident, name, active in pending:
            if not self.open or self.ctx.late():
                break
            note = ""
            with self.ctx.guard("record"):
                note = _text(mpp_notes.text(t.f.get("notes")))
            self.add(ident, name, note, active)

    def add(self, ident: str, name: str, note: str, active: bool):
        if not note:
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


def _tasks(project, ctx, budget, pending) -> list:
    """The Tasks table; each task put is appended to `pending` (task, ID, name, active) for its note."""
    grid = sheet_grid.Grid("Tasks", budget, ctx)
    _put(grid, 0, list(TASK_COLUMNS))
    r = seen = 1
    for t in project.tasks:
        if grid.cut:
            break
        if seen >= limits.SHEET_ROWS:
            grid.stop("rows", limits.SHEET_ROWS)
            break
        seen += 1
        got = None
        with ctx.guard("record"):
            got = _task(project, t, budget.chars)
        if got is None:
            continue
        row, active, ident, name = got
        if not active:
            grid.hide_row(r)
        if not _put(grid, r, row):
            break
        r += 1
        pending.append((t, ident, name, active))
    return grid.blocks()


def _resource(res):
    """(row values, active) of a resource with a name or an ID other than 0, else None."""
    rid, name = res.f.get("id"), _text(res.f.get("name"))
    if not name and not rid:
        return None
    mu = _num(res.max_units)
    row = ["" if rid is None else str(rid), name, res.kind.capitalize(), mu + "%" if mu else ""]
    return row, res.active


def _resources(project, ctx, budget) -> list:
    """The Resources table, or [] when no resource row was put (its header is put with the first row)."""
    grid = sheet_grid.Grid("Resources", budget, ctx)
    r = seen = 1
    for res in project.resources:
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
    return grid.blocks()


def build(project, ctx) -> Doc:
    """The Doc of an mpp_read.Project."""
    title = " ".join(_text(project.title).split())
    parts = [f"{label} {day}" for label, day in (("Start", _day(project.start)), ("Finish", _day(project.finish)),
                                                 ("Status date", _day(project.status))) if day]
    blocks = [Para([Text(DOT.join(parts))])] if parts else []
    budget = sheet_grid.Budget(limits.SHEET_CELLS, limits.SHEET_CHARS)
    pending = []
    blocks += [Heading(2, [Text("Tasks")])] + _tasks(project, ctx, budget, pending)
    res = _resources(project, ctx, budget)
    if res:
        blocks += [Heading(2, [Text("Resources")])] + res
    notes = _Notes(ctx, budget)
    notes.read(pending)
    if notes.items:
        blocks += [Heading(2, [Text("Notes")])] + notes.items
    return Doc(title=title, blocks=blocks)
