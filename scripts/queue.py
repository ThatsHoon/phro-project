#!/usr/bin/env python3
"""The project's queue of owner decisions. Standard library only.

Tickets are Markdown files in <root>/queue/, its own git repository. This script is the only writer: every write
regenerates board.json and commits only its own paths, so sessions writing at once never commit
each other's files. Output is paths and counts, never ticket bodies.

  queue.py init <root>
  queue.py ask <root> --title T --question Q --option O [--option O ...] --priority N --reason R --session UUID [--default D] [--link P ...] [--ticket ID]
  queue.py decide <root> <id> --text T [--priority N]
  queue.py move <root> <id> <status> [--priority N] [--session UUID]
  queue.py done <root> <id>
  queue.py drop <root> <id>        the owner removes a waiting question (closed with a record line)
  queue.py board <root>
  queue.py deliver <root> <id>     prints SEND<TAB>local_<UUID><TAB><message> for the asking session
  queue.py sessions <root>         repairs ticket session values to bare UUIDs
  queue.py serve <root> [--port N] the board page (ui/board.html) on 127.0.0.1, read live, answers and drops written through
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
DROPPED = "dropped by the owner"
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
                     "dropped": t["status"] == "ready" and bool(t["decisions"]) and t["decisions"][-1].endswith(DROPPED),
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
    # the panel's drop button runs this script on this root
    data = {"generated": int(now), "counts": counts, "closed_7d": closed, "inbox": f"/{plugin_name()}:inbox",
            "script": Path(__file__).resolve().as_posix(), "root": Path(root).resolve().as_posix(),
            "attention": attention, "sessions": sessions, "tickets": rows}
    write_atomic(qdir(root) / "board.json", json.dumps(data, ensure_ascii=False, indent=1) + "\n")
    return data


def core_data(root):
    """docs/core at a glance for the board page: final goal, drift, and per document its brief, headings, first line and review age.
    None without core.json."""
    core = Path(root) / "docs/core"
    try:
        manifest = json.loads((core / "core.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    def text_of(name):
        try:
            return (core / name).read_text(encoding="utf-8-sig")
        except OSError:
            return None
    lines_of = lambda text: [l.strip() for l in text.splitlines() if l.strip()]
    goals = text_of("goals.md") or ""
    first = goals.split("\n## ", 2)[1] if "\n## " in goals else ""  # the first section: the final goal, in the owner's words
    goal, label, quoting = [], "", False
    for l in lines_of(first)[1:]:
        if l.startswith(">"):  # quoted lines are the goal itself; the plain line above a block names it
            if not quoting:
                goal.append({"label": label, "quotes": []})
            goal[-1]["quotes"].append(l.lstrip("> ").strip())
            quoting = True
        else:
            label, quoting = l, False
    docs = []
    for doc in manifest.get("docs", []):
        name = str(doc.get("file", ""))
        text = text_of(name)
        reviewed = (manifest.get("reviewed") or {}).get(name)
        row = {"file": name, "brief": str(doc.get("brief", "")), "missing": text is None,
               "reviewed_days": int((time.time() - reviewed) // 86400) if reviewed else None}
        if text is not None:
            body = lines_of(text)
            row.update(lines=len(text.splitlines()), heads=[l[3:] for l in body if l.startswith("## ")][:8],
                       first=next((l for l in body if not l.startswith("#")), "")[:200])
        docs.append(row)
    return {"goals": goal, "drift": str(manifest.get("drift") or ""), "docs": docs}


def view(root):
    """What the board page draws: board.json, plus each waiting question in full with its options, plus the core documents map."""
    data = board(root)
    questions = {}
    for t in data["tickets"]:
        if t["status"] == "needs-owner":
            try:
                full = read_ticket(qdir(root) / "tickets" / f"{t['id']}.md")
                questions[t["id"]] = {"question": full["question"], "options": full["options"]}
            except (ValueError, OSError):
                pass
    return {**data, "questions": questions, "core": core_data(root), "project": Path(root).resolve().name}


def cmd_serve(a):
    """The board page on 127.0.0.1. Reads come from queue/ and docs/core on every request; answers and drops go through the
    same functions as the command line. Writes need the token the page was served with and a local Host and Origin."""
    import secrets
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.request import urlopen
    root, token, lock = Path(a.root), secrets.token_urlsafe(24), threading.Lock()
    try:
        with urlopen(f"http://127.0.0.1:{a.port}/api/ping", timeout=1) as r:  # a server for this root already runs there
            if a.port and json.loads(r.read()).get("root") == root.resolve().as_posix():
                print(f"RUNNING\thttp://127.0.0.1:{a.port}/", flush=True)
                return None
    except (OSError, ValueError):
        pass
    page = (PLUGIN / "ui/board.html").read_text(encoding="utf-8").replace("__TOKEN__", token)
    old = qdir(root) / "inbox.html"  # the static page this server replaces
    if old.exists():
        old.unlink()
        git(root, "rm", "-q", "--cached", "--ignore-unmatch", "inbox.html", check=False)
        git(root, "commit", "-q", "-m", "board page is served now", "--", "inbox.html", check=False)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def local(self):
            port = self.server.server_address[1]
            hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
            origin = self.headers.get("Origin")
            return self.headers.get("Host") in hosts and (origin is None or origin.split("//", 1)[-1] in hosts)

        def send(self, code, body, kind="application/json; charset=utf-8"):
            raw = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.local():
                return self.send(403, {"error": "local requests only"})
            if self.path in ("/", "/index.html"):
                return self.send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/ping":
                return self.send(200, {"root": root.resolve().as_posix()})
            if self.path == "/api/view":
                with lock:
                    return self.send(200, view(root))
            self.send(404, {"error": "not found"})

        def do_POST(self):
            if not self.local() or not secrets.compare_digest(self.headers.get("X-Queue-Token", ""), token):
                return self.send(403, {"error": "this page is out of date: reload it"})
            try:
                body = json.loads(self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536)) or b"{}")
                tid, text = str(body.get("id", "")), str(body.get("text", "")).strip()
            except (ValueError, AttributeError):
                return self.send(400, {"error": "bad request"})
            if not re.fullmatch(r"\d{8}-[a-z0-9-]+", tid):
                return self.send(400, {"error": "bad ticket id"})
            act = argparse.Namespace(root=root, id=tid, text=text, priority=None)
            try:
                with lock:
                    if self.path == "/api/decide":
                        if not text:
                            raise Refused("write an answer or pick an option")
                        cmd_decide(act)
                    elif self.path == "/api/drop":
                        cmd_drop(act)
                    else:
                        return self.send(404, {"error": "not found"})
            except (Refused, ValueError) as e:
                return self.send(409, {"error": str(e)})
            self.send(200, {"ok": True})

    try:
        server = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)  # the port is taken by something else
    print(f"SERVING\thttp://127.0.0.1:{server.server_address[1]}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return None


def commit(root, message, paths):
    """Commit only these paths. Another session may hold the index lock or be replacing board.json, so retry."""
    board(root)
    paths = [p for p in [*paths, "board.json"] if (qdir(root) / p).exists()]
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


def cmd_drop(a):
    """The owner removes a question. It leaves Needs your answer at once; the asking session's hook pulls the drop like a decision,
    and that session closes the ticket. git keeps it, so move can reopen it."""
    path = ticket_path(a.root, a.id)
    t = read_ticket(path)
    t["decisions"].append(f"{time.strftime('%Y%m%d')} {DROPPED}")
    set_status(t, "ready")
    return save(a.root, path, t, f"drop {path.stem}"), None


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
            if t.get("dropped"):
                out.append(f"Ticket {t['id']} was dropped by the owner: stop waiting on it and do not do its work. Close it with {queue} done {r} {t['id']}.")
            else:
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
    s = sub.add_parser("drop", help="the owner removes a waiting question; closes it with a record line")
    s.add_argument("root"); s.add_argument("id"); s.set_defaults(fn=cmd_drop)
    s = sub.add_parser("board"); s.add_argument("root"); s.set_defaults(fn=cmd_board)
    s = sub.add_parser("deliver"); s.add_argument("root"); s.add_argument("id"); s.set_defaults(fn=cmd_deliver)
    s = sub.add_parser("sessions"); s.add_argument("root"); s.set_defaults(fn=cmd_sessions)
    s = sub.add_parser("serve", help="the board page on 127.0.0.1, read live from queue/ and docs/core")
    s.add_argument("root"); s.add_argument("--port", type=int, default=8765); s.set_defaults(fn=cmd_serve)
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
