#!/usr/bin/env python3
"""The project's queue of owner decisions. Standard library only.

Tickets are Markdown files in <root>/queue/, its own git repository. This script is the only writer: every write
regenerates board.json (and inbox.html) and commits only its own paths, so sessions writing at once never commit
each other's files. Output is paths and counts, never ticket bodies.

  queue.py init <root>
  queue.py ask <root> --title T --question Q --option O [--option O ...] --priority N --reason R --session S [--link P ...] [--ticket ID]
  queue.py decide <root> <id> --text T [--priority N]
  queue.py move <root> <id> <status> [--priority N]
  queue.py done <root> <id>
  queue.py board <root>
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
STATES = ("new", "researching", "ready", "doing", "verify", "done", "needs-owner", "blocked")
FIELDS = ("status", "waiting_on", "priority", "priority_reason", "session", "links", "returns")
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
    text = Path(path).read_text(encoding="utf-8")
    m = re.match(r"---\n(.*?)\n---\n(.*)\Z", text, re.S)
    if not m:
        raise ValueError("no frontmatter")
    meta = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(": ")
        if not sep or key not in FIELDS:
            raise ValueError(f"bad line: {line[:40]}")
        meta[key] = value
    if set(meta) != set(FIELDS):
        raise ValueError(f"missing: {', '.join(sorted(set(FIELDS) - set(meta)))}")
    if meta["status"] not in STATES or meta["priority"] not in ("1", "2", "3"):
        raise ValueError("bad status or priority")
    links = json.loads(meta["links"])
    body = m.group(2).splitlines()
    if not body or not body[0].startswith("# "):
        raise ValueError("no title")
    options = [l[2:] for l in body if l.startswith("- ")]
    decisions = [l[len("decision: "):] for l in body if l.startswith("decision: ")]
    question = "\n".join(l for l in body[1:] if l.strip() and not l.startswith(("- ", "decision: "))).strip()
    return {"id": Path(path).stem, "title": body[0][2:], "status": meta["status"], "waiting_on": meta["waiting_on"],
            "priority": int(meta["priority"]), "priority_reason": meta["priority_reason"], "session": meta["session"],
            "links": links, "returns": int(meta["returns"]), "question": question, "options": options,
            "decisions": decisions}


def render_ticket(t):
    meta = dict(t, priority=str(t["priority"]), returns=str(t["returns"]), links=json.dumps(t["links"], ensure_ascii=False))
    head = "\n".join(f"{k}: {meta[k]}" for k in FIELDS)
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
    # ponytail: one git call per ticket; batch with one `git log` if a queue grows past a few hundred tickets
    r = git(root, "log", "--diff-filter=A", "--format=%ct", "--", f"tickets/{path.name}", check=False)
    stamps = r.stdout.split()
    born = int(stamps[-1]) if stamps else int(path.stat().st_mtime)
    return int(time.time()) - born


def board(root):
    """Write board.json from every ticket; tickets that do not parse are reported and left out."""
    rows, counts, closed = [], {s: 0 for s in STATES}, 0
    week = time.time() - 7 * 86400
    for path in sorted((qdir(root) / "tickets").glob("*.md")):
        try:
            t = read_ticket(path)
        except (ValueError, OSError) as e:
            print(f"SKIPPED\t{path.as_posix()}\t{e}")
            continue
        counts[t["status"]] += 1
        if t["status"] == "done" and path.stat().st_mtime >= week:  # ponytail: done time = file time, add a field if edits blur it
            closed += 1
        rows.append({"id": t["id"], "title": t["title"], "status": t["status"], "priority": t["priority"],
                     "session": t["session"], "age_s": age_of(root, path), "returns": t["returns"],
                     "last_line": last_line(root, t["links"]) if t["status"] in ("doing", "verify") else ""})
    rows.sort(key=lambda r: (r["priority"], -r["age_s"]))
    data = {"generated": int(time.time()), "counts": counts, "closed_7d": closed, "tickets": rows}
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


def render_inbox(root, data):
    """One self-contained page, in the panel's order. Every ticket field is escaped."""
    from html import escape
    by_id = {}
    for t in data["tickets"]:
        if t["status"] == "needs-owner":
            try:
                by_id[t["id"]] = read_ticket(qdir(root) / "tickets" / f"{t['id']}.md")
            except (ValueError, OSError):
                pass
    meta = lambda t: f'<div class="meta">P{t["priority"]} · {escape(t["session"])} · {t["age_s"] // 86400} d · {t["id"]}</div>'
    waiting, running = [], []
    for t in data["tickets"]:
        if t["status"] == "needs-owner" and t["id"] in by_id:
            full = by_id[t["id"]]
            opts = "".join(f"<li>{escape(o)}</li>" for o in full["options"])
            waiting.append(f'<div class="card{" p1" if t["priority"] == 1 else ""}"><b>{escape(t["title"])}</b>{meta(t)}'
                           f'<p>{escape(full["question"])}</p><ul>{opts}</ul></div>')
        elif t["status"] in ("doing", "verify"):
            running.append(f'<div class="card"><b>{escape(t["title"])}</b> <span class="meta">{t["status"]}</span>{meta(t)}'
                           f'<p>{escape(t["last_line"])}</p></div>')
    attention = sum(1 for t in data["tickets"] if t["status"] == "blocked" or t["returns"] >= 3)
    sections = "\n".join([
        f"<h2>Waiting on you ({len(waiting)})</h2>" + ("".join(waiting) or '<p class="meta">Nothing waiting.</p>'),
        f"<h2>Running ({len(running)})</h2>" + ("".join(running) or '<p class="meta">Nothing running.</p>'),
        f"<h2>Attention</h2><p>blocked {data['counts']['blocked']} · returned 3 times {attention - data['counts']['blocked']}</p>",
        f"<h2>Done in 7 days</h2><p>{data['closed_7d']}</p>",
    ])
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
    write_atomic(path, render_ticket(t))
    commit(root, message, [f"tickets/{path.name}"])
    return path


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
        if f"/{QUEUE}/" not in old.split() and f"{QUEUE}/" not in old.split():
            exclude.write_text(old + ("" if old.endswith("\n") or not old else "\n") + f"{QUEUE}/\n", encoding="utf-8")
    (q / "tickets").mkdir(parents=True, exist_ok=True)
    git(a.root, "init", "-q")
    git(a.root, "config", "core.autocrlf", "false")
    commit(a.root, "init queue", [])
    return q, None


def cmd_ask(a):
    if a.ticket:
        path = ticket_path(a.root, a.ticket)
        t = read_ticket(path)
        if t["decisions"] and t["question"] == a.question:  # ponytail: checks the current question only, not earlier ones
            raise Refused(f"already decided: {path.as_posix()}")
        if a.title:
            t["title"] = a.title
        t.update(question=a.question, options=a.option, priority=a.priority, priority_reason=a.reason, session=a.session)
        t["links"] = t["links"] + [l for l in links_of(a) if l not in t["links"]]
        set_status(t, "needs-owner")
    else:
        if not a.title:
            raise Refused("--title is needed for a new ticket")
        tickets = qdir(a.root) / "tickets"
        path = tickets / f"{slug(a.title, time.strftime('%Y%m%d'), tickets)}.md"
        t = {"title": a.title, "status": "needs-owner", "waiting_on": "owner", "priority": a.priority,
             "priority_reason": a.reason, "session": a.session, "links": links_of(a), "returns": 0,
             "question": a.question, "options": a.option, "decisions": []}
    return save(a.root, path, t, f"ask {path.stem}"), None


def links_of(a):
    return [l.replace("\\", "/") for l in (a.link or [])]


def cmd_decide(a):
    path = ticket_path(a.root, a.id)
    t = read_ticket(path)
    t["decisions"].append(f"{time.strftime('%Y%m%d')} {a.text}")
    if a.priority:
        t["priority"] = a.priority
    t["status"], t["waiting_on"] = "ready", "agent"
    return save(a.root, path, t, f"decide {path.stem}"), None


def cmd_move(a):
    path = ticket_path(a.root, a.id)
    t = read_ticket(path)
    if a.priority:
        t["priority"] = a.priority
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


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    prio = dict(type=int, choices=(1, 2, 3))
    s = sub.add_parser("init"); s.add_argument("root"); s.set_defaults(fn=cmd_init)
    s = sub.add_parser("ask"); s.add_argument("root"); s.set_defaults(fn=cmd_ask)
    s.add_argument("--title"); s.add_argument("--question", required=True); s.add_argument("--option", action="append", required=True)
    s.add_argument("--priority", required=True, **prio); s.add_argument("--reason", required=True)
    s.add_argument("--session", required=True); s.add_argument("--link", action="append"); s.add_argument("--ticket")
    s = sub.add_parser("decide"); s.add_argument("root"); s.add_argument("id"); s.set_defaults(fn=cmd_decide)
    s.add_argument("--text", required=True); s.add_argument("--priority", **prio)
    s = sub.add_parser("move"); s.add_argument("root"); s.add_argument("id"); s.add_argument("status", choices=STATES)
    s.add_argument("--priority", **prio); s.set_defaults(fn=cmd_move)
    s = sub.add_parser("done"); s.add_argument("root"); s.add_argument("id"); s.set_defaults(fn=cmd_done)
    s = sub.add_parser("board"); s.add_argument("root"); s.set_defaults(fn=cmd_board)
    a = p.parse_args(argv)
    a.root = Path(a.root).resolve()
    if a.cmd != "init" and not (qdir(a.root) / ".git").is_dir():
        print(f"REFUSED\tno queue at {qdir(a.root).as_posix()}: run queue.py init first")
        return 1
    try:
        path, note = a.fn(a)
    except (Refused, ValueError) as e:
        print(f"REFUSED\t{e}")
        return 1
    print("\t".join(["OK", a.cmd, Path(path).as_posix()] + ([note] if note else [])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
