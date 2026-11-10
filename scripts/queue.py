#!/usr/bin/env python3
"""The project's queue of owner decisions. Standard library only.

Tickets are Markdown files in <root>/queue/, its own git repository. This script is the only writer: every write
regenerates board.json (and inbox.html) and commits only its own paths, so sessions writing at once never commit
each other's files. Output is paths and counts, never ticket bodies.

  queue.py init <root>
  queue.py ask <root> --title T --question Q --option O [--option O ...] --priority N --reason R --session UUID [--default D] [--link P ...] [--ticket ID]
  queue.py decide <root> <id> --text T [--priority N]
  queue.py move <root> <id> <status> [--priority N] [--session UUID]
  queue.py done <root> <id>
  queue.py board <root>
  queue.py deliver <root> <id>     prints SEND<TAB>local_<UUID><TAB><message> for the asking session
  queue.py sessions <root>         repairs ticket session values to bare UUIDs
  queue.py hook                    SessionStart / UserPromptSubmit hook: event JSON on stdin
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

QUEUE = "queue"
PLUGIN = Path(__file__).resolve().parents[1]
STATES = ("new", "researching", "ready", "doing", "verify", "done", "needs-owner", "blocked")
FIELDS = ("status", "waiting_on", "priority", "priority_reason", "session", "links", "returns")
OPTIONAL = ("default",)  # tickets written before it still parse
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
TICKET_ID = re.compile(r"\b\d{8}-[a-z0-9-]+\b")
SELF = "self:"
HOOK_LINES = 10
WORKING_S = 600  # a transcript written this recently counts as a working session
CLOSE_OR_SHRINK = "Close this ticket or shrink it to one step?"
CLOSE_OPTIONS = ["A: close it", "B: shrink it to one step"]
LINE_MAX = 120


class Refused(Exception):
    pass


def qdir(root):
    return Path(root) / QUEUE


def git(root, *args, check=True):
    r = subprocess.run(["git", "-C", str(qdir(root)), *args], capture_output=True, text=True, encoding="utf-8")
    if check and r.returncode:
        raise Refused(f"git {args[0]}: {(r.stderr or r.stdout).strip().splitlines()[-1:]}")
    return r


def write_atomic(path, text):
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    for _ in range(40):  # Windows refuses the replace while another process has the target open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    tmp.unlink(missing_ok=True)
    raise Refused(f"could not replace {path.as_posix()}: in use")


def read_ticket(path):
    """Parse one ticket; ValueError when it is not one."""
    return parse_ticket(Path(path).read_text(encoding="utf-8"), Path(path).stem)


def parse_ticket(text, tid):
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if not m:
        raise ValueError("no frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(": ")
        if not sep or key not in FIELDS + OPTIONAL:
            raise ValueError(f"bad line: {line[:40]}")
        meta[key] = value
    if not set(FIELDS) <= set(meta):
        raise ValueError(f"missing: {', '.join(sorted(set(FIELDS) - set(meta)))}")
    if meta["status"] not in STATES or meta["priority"] not in ("1", "2", "3"):
        raise ValueError("bad status or priority")
    links = json.loads(meta["links"])
    if not isinstance(links, list) or not all(isinstance(l, str) for l in links):
        raise ValueError("links is not a list of paths")
    body = m.group(2).splitlines()
    if not body or not body[0].startswith("# "):
        raise ValueError("no title")
    options = [l[2:] for l in body if l.startswith("- ")]
    decisions = [l[len("decision: "):] for l in body if l.startswith("decision: ")]
    question = "\n".join(l for l in body[1:] if l.strip() and not l.startswith(("- ", "decision: "))).strip()
    return {"id": tid, "title": body[0][2:], "status": meta["status"], "waiting_on": meta["waiting_on"],
            "priority": int(meta["priority"]), "priority_reason": meta["priority_reason"], "session": meta["session"],
            "links": links, "returns": int(meta["returns"]), "default": meta.get("default", ""), "question": question,
            "options": options, "decisions": decisions}


def render_ticket(t):
    meta = dict(t, priority=str(t["priority"]), returns=str(t["returns"]), links=json.dumps(t["links"], ensure_ascii=False))
    head = "\n".join(f"{k}: {meta[k]}" for k in FIELDS + OPTIONAL if k in FIELDS or meta.get(k))
    body = [f"# {t['title']}", "", t["question"], ""] + [f"- {o}" for o in t["options"]]
    if t["decisions"]:
        body += [""] + [f"decision: {d}" for d in t["decisions"]]
    return f"---\n{head}\n---\n" + "\n".join(body) + "\n"


def slug(title, day, tickets):
    base = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:40].strip("-")
    taken = {p.stem for p in tickets.glob("*.md")}
    if not base:
        n = 1
        while f"{day}-t{n}" in taken:
            n += 1
        return f"{day}-t{n}"
    name, n = f"{day}-{base}", 2
    while name in taken:
        name, n = f"{day}-{base}-{n}", n + 1
    return name


def set_status(t, status):
    """A move from doing or verify back to ready or needs-owner is a return; the third turns the question into close or shrink."""
    if t["status"] in ("doing", "verify") and status in ("ready", "needs-owner"):
        t["returns"] += 1
        if t["returns"] == 3:
            t["question"], t["options"], status = CLOSE_OR_SHRINK, list(CLOSE_OPTIONS), "needs-owner"
    t["status"] = status
    t["waiting_on"] = "owner" if status == "needs-owner" else "agent"


def last_line(root, links):
    for link in links:
        p = Path(link) if Path(link).is_absolute() else Path(root) / link
        if p.is_file():
            with p.open("rb") as f:
                f.seek(max(0, p.stat().st_size - 65536))
                lines = [l.strip() for l in f.read().decode("utf-8", "replace").splitlines() if l.strip()]
            return lines[-1][:LINE_MAX] if lines else ""
    return ""


def age_of(root, path):
    # lean: one git call per ticket | when: queue > 300 tickets | fix: batch with one git log
    r = git(root, "log", "--diff-filter=A", "--format=%ct", "--", f"tickets/{path.name}", check=False)
    stamps = r.stdout.split()
    born = int(stamps[-1]) if stamps else int(path.stat().st_mtime)
    return int(time.time()) - born


def clean(text):
    return re.sub(r"[\r\n`]+", " ", str(text)).strip()


def norm_session(value):
    """The canonical session value is the bare transcript UUID, lowercase; the desktop app's local_<UUID> is accepted."""
    v = (value or "").strip().lower()
    v = v[len("local_"):] if v.startswith("local_") else v
    if not UUID.fullmatch(v):
        raise Refused("--session takes this session's id, a UUID or local_<UUID>; the queue hook prints it at session start")
    return v


def transcripts(root):
    """The folder of this project's session transcripts, one <UUID>.jsonl per session."""
    return Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve()))


def session_info(root, sid):
    """Title from the last custom-title in the transcript's tail, last activity from its mtime; gone without a transcript."""
    path = transcripts(root) / f"{sid}.jsonl"
    try:
        size, mtime = path.stat().st_size, int(path.stat().st_mtime)
    except (OSError, ValueError):
        return {"title": clean(sid)[:8], "last_active": None, "gone": True}
    title = ""
    try:
        with path.open("rb") as f:
            f.seek(max(0, size - 65536))
            for line in f.read().decode("utf-8", "replace").splitlines():
                if '"custom-title"' in line:
                    try:
                        title = json.loads(line).get("customTitle") or title
                    except (ValueError, AttributeError):
                        pass  # the first line of the tail is usually cut
    except OSError:
        pass
    return {"title": clean(title)[:80] or sid[:8], "last_active": mtime, "gone": False}


def plugin_name():
    try:
        return json.loads((PLUGIN / ".claude-plugin/plugin.json").read_text(encoding="utf-8"))["name"]
    except (OSError, ValueError, KeyError):
        return "phro-project"


def board(root):
    """Write board.json from every ticket; tickets that do not parse are reported and left out."""
    rows, counts, closed = [], {s: 0 for s in STATES}, 0
    now = time.time()
    week = now - 7 * 86400
    for path in sorted((qdir(root) / "tickets").glob("*.md")):
        try:
            t = read_ticket(path)
        except (ValueError, OSError) as e:
            print(f"SKIPPED\t{path.as_posix()}\t{e}")
            continue
        counts[t["status"]] += 1
        changed = path.stat().st_mtime  # lean: last change = file mtime | when: a copy or checkout touches tickets | fix: a changed_at field
        if t["status"] == "done" and changed >= week:
            closed += 1
        rows.append({"id": t["id"], "title": t["title"], "status": t["status"], "priority": t["priority"],
                     "session": t["session"], "age_s": age_of(root, path), "changed_s": int(now - changed), "returns": t["returns"],
                     "can_wait": bool(t["default"]), "default": t["default"],
                     "question": t["question"][:300] if t["status"] == "needs-owner" else "",
                     "last_line": last_line(root, t["links"]) if t["status"] in ("doing", "verify") else ""})
    rows.sort(key=lambda r: (r["priority"], -r["age_s"]))
    sessions = {sid: session_info(root, sid) for sid in sorted({r["session"] for r in rows if r["status"] != "done"})}
    for r in rows:
        last = sessions.get(r["session"], {}).get("last_active")
        # the asking turn writes for a while after the ask, so only activity well after it counts
        r["still_working"] = bool(r["status"] == "needs-owner" and last and last > now - r["changed_s"] + 300)
    open_rows = [r for r in rows if r["status"] != "done"]
    attention = {"gone": sum(1 for r in open_rows if sessions[r["session"]]["gone"]),
                 "returned": sum(1 for r in open_rows if r["returns"] >= 3),
                 "stale_ready": sum(1 for r in rows if r["status"] == "ready" and r["changed_s"] > 86400)}
    data = {"generated": int(now), "counts": counts, "closed_7d": closed, "inbox": f"/{plugin_name()}:inbox",
            "attention": attention, "sessions": sessions, "tickets": rows}
    write_atomic(qdir(root) / "board.json", json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    render_inbox(root, data)
    return data


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Work queue</title>
<style>
:root { --bg: #fafaf9; --fg: #1c1917; --muted: #78716c; --card: #ffffff; --line: #e7e5e4; --accent: #b45309; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) { --bg: #1c1917; --fg: #f5f5f4; --muted: #a8a29e; --card: #292524; --line: #44403c; --accent: #f59e0b; } }
:root[data-theme="dark"] { --bg: #1c1917; --fg: #f5f5f4; --muted: #a8a29e; --card: #292524; --line: #44403c; --accent: #f59e0b; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 15px/1.5 system-ui, sans-serif; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 20px; margin: 0 0 4px; } h2 { font-size: 15px; margin: 28px 0 8px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 8px; padding: 12px 14px; margin: 8px 0; overflow-wrap: anywhere; }
.meta { color: var(--muted); font-size: 13px; } .p1 { border-left: 4px solid var(--accent); }
ul { margin: 6px 0 0; padding-left: 20px; } p { margin: 6px 0; }
</style></head><body><main>
<h1>Work queue</h1><div class="meta">{stamp}</div>
{sections}
</main></body></html>
"""


def core_section(root):
    """A rough map of docs/core: the final goal, the drift line, and per document its brief, headings and first sentence. Empty without core.json."""
    from html import escape
    core = Path(root) / "docs/core"
    try:
        manifest = json.loads((core / "core.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    def text_of(name):
        try:
            return (core / name).read_text(encoding="utf-8-sig")
        except OSError:
            return None
    lines_of = lambda text: [l.strip() for l in text.splitlines() if l.strip()]
    goals = text_of("goals.md") or ""
    goal = next((l.lstrip("> ").strip() for l in lines_of(goals.split("\n## ", 2)[1] if "\n## " in goals else "")[1:]), "")
    top = "".join(f"<p><b>{label}</b> {escape(value)}</p>" for label, value in (("Final goal", goal), ("Drift", manifest.get("drift") or ""))
                  if value)
    cards = []
    for doc in manifest.get("docs", []):
        name = str(doc.get("file", ""))
        text = text_of(name)
        reviewed = (manifest.get("reviewed") or {}).get(name)
        age = "never reviewed" if not reviewed else f"reviewed {int((time.time() - reviewed) // 86400)} d ago"
        if text is None:
            cards.append(f'<div class="card"><b>{escape(name)}</b> <span class="meta">missing</span><p>{escape(str(doc.get("brief", "")))}</p></div>')
            continue
        body = lines_of(text)
        heads = [l[3:] for l in body if l.startswith("## ")]
        first = next((l for l in body if not l.startswith("#")), "")
        cards.append(f'<div class="card"><b>{escape(name)}</b> <span class="meta">{escape(str(doc.get("brief", "")))}</span>'
                     f'<div class="meta">{len(text.splitlines())} lines · {age}</div>'
                     + (f'<p class="meta">{escape(" · ".join(heads[:8]))}</p>' if heads else "")
                     + (f"<p>{escape(first[:200])}</p>" if first else "") + "</div>")
    return f"<h2>Core documents ({len(cards)})</h2>{top}" + "".join(cards)


def ago(s):
    s = max(0, int(s))
    return f"{s // 86400} d" if s >= 86400 else f"{s // 3600} h" if s >= 3600 else f"{max(1, s // 60)} m"


def render_inbox(root, data):
    """One self-contained page, in the panel's order. Every ticket field is escaped."""
    from html import escape
    sessions = data.get("sessions", {})
    who = lambda t: escape(sessions.get(t["session"], {}).get("title") or t["session"][:8])
    by_id = {}
    for t in data["tickets"]:
        if t["status"] == "needs-owner":
            try:
                by_id[t["id"]] = read_ticket(qdir(root) / "tickets" / f"{t['id']}.md")
            except (ValueError, OSError):
                pass
    blocking, can_wait, decided, running = [], [], [], []
    for t in data["tickets"]:
        meta = f'<div class="meta">P{t["priority"]} · {who(t)} · {ago(t["age_s"])} · {t["id"]}</div>'
        if t["status"] == "needs-owner" and t["id"] in by_id:
            full = by_id[t["id"]]
            opts = "".join(f"<li>{escape(o)}</li>" for o in full["options"])
            if t.get("can_wait"):
                can_wait.append(f'<div class="card"><b>{escape(t["title"])}</b>{meta}<p>{escape(full["question"])}</p><ul>{opts}</ul>'
                                f'<p class="meta">Without an answer: {escape(t["default"])}</p></div>')
            else:
                state = "still working" if t.get("still_working") else "stopped"
                blocking.append(f'<div class="card{" p1" if t["priority"] == 1 else ""}"><b>{escape(t["title"])}</b>{meta}'
                                f'<p>{escape(full["question"])}</p><ul>{opts}</ul><p class="meta">Session {state}</p></div>')
        elif t["status"] == "ready":
            late = " p1" if t.get("changed_s", 0) > 86400 else ""
            decided.append(f'<div class="card{late}"><b>{escape(t["title"])}</b> <span class="meta">decided {ago(t.get("changed_s", 0))} ago, '
                           f'not picked up</span>{meta}</div>')
        elif t["status"] in ("doing", "verify"):
            running.append(f'<div class="card"><b>{escape(t["title"])}</b> <span class="meta">{t["status"]}</span>{meta}'
                           f'<p>{escape(t["last_line"])}</p></div>')
    now = data["generated"]
    people = []
    for sid, s in sessions.items():
        mine = [t for t in data["tickets"] if t["session"] == sid and t["status"] != "done"]
        state = "gone" if s["gone"] else "working" if now - (s["last_active"] or 0) < WORKING_S else f"idle {ago(now - s['last_active'])}"
        people.append(f'<p><b>{escape(s["title"])}</b> <span class="meta">{state} · {len(mine)} open</span></p>')
    at = data.get("attention", {})
    notes = [f"{n} {label}" for n, label in ((at.get("gone", 0), "with the asking session gone"), (at.get("returned", 0), "returned 3 times"),
                                             (data["counts"].get("blocked", 0), "blocked")) if n]
    sections = "\n".join(s for s in [
        f"<h2>Needs your answer ({len(blocking)})</h2>" + ("".join(blocking) or '<p class="meta">Nothing is waiting on you.</p>'),
        f"<details><summary><h2 style=\"display:inline\">Can wait ({len(can_wait)})</h2></summary>{''.join(can_wait)}</details>" if can_wait else "",
        f"<h2>Decided, not picked up ({len(decided)})</h2>" + "".join(decided) if decided else "",
        f"<h2>In progress ({len(running)})</h2>" + "".join(running) if running else "",
        f"<h2>Sessions ({len(people)})</h2>" + "".join(people) if people else "",
        f"<h2>Attention</h2><p>{' · '.join(notes)}</p>" if notes else "",
        f"<h2>Done in 7 days</h2><p>{data['closed_7d']}</p>" if data["closed_7d"] else "",
        core_section(root),
    ] if s)
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(data["generated"]))
    path = qdir(root) / "inbox.html"
    write_atomic(path, PAGE.replace("{stamp}", stamp).replace("{sections}", sections))
    return path


def commit(root, message, paths):
    """Commit only these paths. Another session may hold the index lock or be replacing board.json, so retry."""
    board(root)
    paths = [p for p in [*paths, "board.json", "inbox.html"] if (qdir(root) / p).exists()]
    for _ in range(10):
        add = git(root, "add", "--", *paths, check=False)
        r = add if add.returncode else git(root, "commit", "-q", "-m", message, "--", *paths, check=False)
        if not r.returncode:
            return
        time.sleep(0.5)
    err = [l for l in (r.stderr + r.stdout).splitlines() if not l.startswith("warning:")] or ["unknown"]
    if any("index.lock" in l for l in err):
        raise Refused(f"git index locked: {(qdir(root) / '.git/index.lock').as_posix()}")
    raise Refused(f"git: {err[-1][:200]}")


def ticket_path(root, tid):
    p = qdir(root) / "tickets" / f"{tid}.md"
    if not p.is_file():
        raise Refused(f"no ticket {tid}")
    return p


def save(root, path, t, message):
    text = render_ticket(t)
    try:  # a newline in a one-line field would turn into another field or a decision
        back = parse_ticket(text, path.stem)
    except ValueError as e:
        raise Refused(f"a field would not read back: {e}")
    if any(back[k] != t[k] for k in back if k != "id"):
        raise Refused("a field would not read back the same: keep titles, options, reasons and answers on one line")
    write_atomic(path, text)
    commit(root, message, [f"tickets/{path.name}"])
    return path


def git_out(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, encoding="utf-8").stdout.strip()


def cmd_init(a):
    q = qdir(a.root)
    outer = subprocess.run(["git", "-C", str(a.root), "rev-parse", "--git-path", "info/exclude"],
                           capture_output=True, text=True, encoding="utf-8")
    if (q / ".git").is_dir():
        return q, "exists"
    if not outer.returncode:  # the project is a git repo: keep the queue out of its status
        exclude = Path(outer.stdout.strip())
        exclude = exclude if exclude.is_absolute() else Path(a.root) / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        old = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        prefix = git_out(a.root, "rev-parse", "--show-prefix")  # the root may sit inside the outer repo
        entry = f"/{prefix}{QUEUE}/"  # anchored, so a src/queue/ elsewhere stays visible
        if entry not in old.split():
            exclude.write_text(old + ("" if old.endswith("\n") or not old else "\n") + entry + "\n", encoding="utf-8")
    (q / "tickets").mkdir(parents=True, exist_ok=True)
    git(a.root, "init", "-q")
    git(a.root, "config", "core.autocrlf", "false")
    commit(a.root, "init queue", [])
    return q, None


def cmd_ask(a):
    if a.ticket:
        path = ticket_path(a.root, a.ticket)
        t = read_ticket(path)
        if t["decisions"] and t["question"] == a.question:  # lean: dedup checks the current question only | when: an old question is asked again | fix: compare every past question
            raise Refused(f"already decided: {path.as_posix()}")
        if a.title:
            t["title"] = a.title
        t.update(question=one_block(a.question), options=a.option, priority=a.priority, priority_reason=a.reason,
                 session=norm_session(a.session), default=a.default or "")
        t["links"] = t["links"] + [l for l in links_of(a) if l not in t["links"]]
        set_status(t, "needs-owner")
    else:
        if not a.title:
            raise Refused("--title is needed for a new ticket")
        tickets = qdir(a.root) / "tickets"
        path = tickets / f"{slug(a.title, time.strftime('%Y%m%d'), tickets)}.md"
        t = {"title": a.title, "status": "needs-owner", "waiting_on": "owner", "priority": a.priority,
             "priority_reason": a.reason, "session": norm_session(a.session), "links": links_of(a), "returns": 0,
             "default": a.default or "", "question": one_block(a.question), "options": a.option, "decisions": []}
    return save(a.root, path, t, f"ask {path.stem}"), None


def one_block(text):
    """The question as it reads back: no blank lines, no outer spaces."""
    return "\n".join(l for l in text.splitlines() if l.strip()).strip()


def links_of(a):
    return [l.replace("\\", "/") for l in (a.link or [])]


def cmd_decide(a):
    path = ticket_path(a.root, a.id)
    t = read_ticket(path)
    t["decisions"].append(f"{time.strftime('%Y%m%d')} {a.text}")
    if a.priority:
        t["priority"] = a.priority
    # a session that went past its own question records what it did; nothing is left for anyone to pick up
    t["status"], t["waiting_on"] = ("done" if a.text.strip().startswith(SELF) else "ready"), "agent"
    return save(a.root, path, t, f"decide {path.stem}"), None


def cmd_move(a):
    path = ticket_path(a.root, a.id)
    t = read_ticket(path)
    if a.priority:
        t["priority"] = a.priority
    if a.session:
        t["session"] = norm_session(a.session)
    if a.status != t["status"]:
        set_status(t, a.status)
    return save(a.root, path, t, f"move {path.stem} {t['status']}"), None


def cmd_done(a):
    path = ticket_path(a.root, a.id)
    t = read_ticket(path)
    set_status(t, "done")
    return save(a.root, path, t, f"done {path.stem}"), None


def cmd_board(a):
    data = board(a.root)
    return qdir(a.root) / "board.json", f"{len(data['tickets'])} tickets"


def cmd_deliver(a):
    """Only the session holds the messaging tool, so this prints the target and the exact message for it to send."""
    path = ticket_path(a.root, a.id)
    t = read_ticket(path)
    if not UUID.fullmatch(t["session"]):
        raise Refused(f"the ticket's session {clean(t['session'])[:40]!r} is not a session id: run queue.py sessions first")
    if t["status"] == "needs-owner":
        raise Refused("not decided yet: run decide first")
    print(f"SEND\tlocal_{t['session']}\tticket {t['id']} decided, read the ticket file {path.resolve().as_posix()}")
    return None


def cmd_sessions(a):
    """Rewrite each ticket's session to the bare UUID: strip local_, resolve a prefix that names exactly one transcript."""
    names = [p.stem.lower() for p in transcripts(a.root).glob("*.jsonl")]
    changed = []
    for path in sorted((qdir(a.root) / "tickets").glob("*.md")):
        try:
            t = read_ticket(path)
        except (ValueError, OSError):
            continue
        v = t["session"].strip().lower()
        v = v[len("local_"):] if v.startswith("local_") else v
        if not UUID.fullmatch(v):
            hits = [n for n in names if n.startswith(v)] if re.fullmatch(r"[0-9a-f][0-9a-f-]{3,}", v) else []
            if len(hits) != 1:
                print(f"LEFT\t{t['id']}\t{clean(t['session'])[:40]}\t{len(hits)} matching transcripts")
                continue
            v = hits[0]
        if v != t["session"]:
            t["session"] = v
            write_atomic(path, render_ticket(t))
            changed.append(f"tickets/{path.name}")
    if changed:
        commit(a.root, f"sessions {len(changed)}", changed)
    else:
        board(a.root)
    return qdir(a.root) / "board.json", f"{len(changed)} fixed"


RULE = ("When you need the owner's decision, open a ticket with `{queue} ask {root} --session {sid}` (add --help for its options) "
        "and also ask in chat in one line naming the ticket id. Give --default \"<what you will do without an answer>\" whenever going on is reasonable, "
        "and go on with it; a ticket without a default blocks, so stop after asking. "
        "Record whichever answer comes first with `{queue} decide {root} <id> --text <answer>`. "
        "When you go past your own ticket, close it with `{queue} done {root} <id>`, or decide it with --text \"self: <what you did>\". "
        "When the owner answers here a ticket another session owns, record it with decide, run `{queue} deliver {root} <id>` "
        "and send the printed message to the printed session; do not do that ticket's work here. "
        "If an answer names no ticket id and this session owns no waiting ticket, ask the owner for the id in one line.")


def find_root(start):
    here = Path(start).resolve()
    return next((p for p in (here, *here.parents) if (qdir(p) / "board.json").is_file()), None)


def hook_lines(event):
    """Reads board.json only. The session's own decided tickets on every event; tickets of other sessions named in the prompt."""
    sid = str(event.get("session_id") or "").lower()
    root = find_root(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    if not root or not UUID.fullmatch(sid):
        return []
    data = json.loads((qdir(root) / "board.json").read_text(encoding="utf-8"))
    rows = [t for t in data.get("tickets", []) if isinstance(t, dict) and "id" in t]
    queue, r = f'python -I -X utf8 "{Path(__file__).resolve().as_posix()}"', f'"{root.as_posix()}"'
    out, name = [], event.get("hook_event_name")
    if name == "SessionStart":
        out.append(RULE.format(queue=queue, root=r, sid=sid))
        mine = [t for t in rows if t.get("session") == sid and t.get("status") in ("needs-owner", "doing", "verify", "blocked")]
        if mine:
            out.append("Your open tickets: " + ", ".join(f"{t['id']} ({t.get('status')})" for t in mine[:8])
                       + ". Close any you already went past, as the rule says.")
    for t in rows:
        if t.get("session") == sid and t.get("status") == "ready":
            path = (qdir(root) / "tickets" / f"{t['id']}.md").as_posix()
            out.append(f"Ticket {t['id']} was decided for you: read {path}, then run {queue} move {r} {t['id']} doing.")
    if name == "UserPromptSubmit":
        by_id = {t["id"]: t for t in rows}
        for tid in dict.fromkeys(TICKET_ID.findall(str(event.get("prompt") or ""))):
            t = by_id.get(tid)
            if not t or t.get("session") == sid or t.get("status") == "done":
                continue
            owner = clean(t.get("session"))[:40]
            title = clean((data.get("sessions") or {}).get(owner, {}).get("title") or owner[:8])[:80]
            out.append(f'Ticket {tid} belongs to session {owner} ("{title}"), not this one. Record the owner\'s answer with '
                       f'{queue} decide {r} {tid} --text "<answer>", then deliver: {queue} deliver {r} {tid}. Do not do that ticket\'s work here.')
    return out[:HOOK_LINES]


def hook():
    """A hook must never break a session: every failure ends silently with exit 0."""
    try:
        lines = hook_lines(json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}"))
    except Exception:
        return 0
    if lines:
        sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0


def main(argv=None):
    if (argv if argv is not None else sys.argv[1:])[:1] == ["hook"]:
        return hook()
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    prio = dict(type=int, choices=(1, 2, 3))
    s = sub.add_parser("init"); s.add_argument("root"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("ask"); s.add_argument("root"); s.set_defaults(fn=cmd_ask)
    s.add_argument("--title"); s.add_argument("--question", required=True); s.add_argument("--option", action="append", required=True)
    s.add_argument("--priority", required=True, **prio); s.add_argument("--reason", required=True)
    s.add_argument("--session", required=True, help="this session's id, a UUID or local_<UUID>")
    s.add_argument("--default", help="what the session does without an answer; makes the ticket can-wait")
    s.add_argument("--link", action="append"); s.add_argument("--ticket")
    s = sub.add_parser("decide"); s.add_argument("root"); s.add_argument("id"); s.set_defaults(fn=cmd_decide)
    s.add_argument("--text", required=True, help='the answer; "self: ..." when the asking session went past it (closes the ticket)')
    s.add_argument("--priority", **prio)
    s = sub.add_parser("move"); s.add_argument("root"); s.add_argument("id"); s.add_argument("status", choices=STATES)
    s.add_argument("--priority", **prio); s.add_argument("--session", help="reassign to this session"); s.set_defaults(fn=cmd_move)
    s = sub.add_parser("done"); s.add_argument("root"); s.add_argument("id"); s.set_defaults(fn=cmd_done)
    s = sub.add_parser("board"); s.add_argument("root"); s.set_defaults(fn=cmd_board)
    s = sub.add_parser("deliver"); s.add_argument("root"); s.add_argument("id"); s.set_defaults(fn=cmd_deliver)
    s = sub.add_parser("sessions"); s.add_argument("root"); s.set_defaults(fn=cmd_sessions)
    a = p.parse_args(argv)
    a.root = Path(a.root).resolve()
    if a.cmd != "init" and not (qdir(a.root) / ".git").is_dir():
        print(f"REFUSED\tno queue at {qdir(a.root).as_posix()}: run queue.py init first")
        return 1
    try:
        done = a.fn(a)
    except (Refused, ValueError) as e:
        print(f"REFUSED\t{e}")
        return 1
    if done is None:  # the command printed its own result
        return 0
    path, note = done
    print("\t".join(["OK", a.cmd, Path(path).as_posix()] + ([note] if note else [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
