"""The work queue: tickets in their own git repo, one-path commits, no re-asking, the third return, the board.

Run: python -I -X utf8 tests/test_queue.py
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
SCRIPT = HERE / "scripts" / "queue.py"
TODAY = time.strftime("%Y%m%d")
CLOSE_OR_SHRINK = "Close this ticket or shrink it to one step?"


A = "aaaaaaaa-1111-4111-8111-111111111111"
B = "bbbbbbbb-2222-4222-8222-222222222222"
HOME = Path(tempfile.mkdtemp())  # transcripts live under a fake home, never the real one
ENV = {**os.environ, "HOME": str(HOME), "USERPROFILE": str(HOME), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
ENV.pop("CLAUDE_PROJECT_DIR", None)


def q(root, *args):
    return subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), args[0], str(root), *args[1:]],
                          capture_output=True, text=True, encoding="utf-8", env=ENV)


def hook(root, sid, event, prompt=None, stdin=None):
    data = stdin if stdin is not None else json.dumps({"session_id": sid, "cwd": str(root), "hook_event_name": event, "prompt": prompt})
    r = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "hook"], input=data, capture_output=True,
                       text=True, encoding="utf-8", env=ENV)
    assert r.returncode == 0 and not r.stderr, r.stderr
    return r.stdout


def transcript(root, sid, title=None, mtime=None):
    folder = HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve()))
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    filler = json.dumps({"type": "user", "text": "x" * 70000})  # pushes an early title out of the 64 KB tail
    lines = [json.dumps({"type": "custom-title", "customTitle": "old title"}), filler]
    if title:
        lines.append(json.dumps({"type": "custom-title", "customTitle": title, "sessionId": sid}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


def path_of(result):
    assert result.returncode == 0, result.stdout + result.stderr
    line = [l for l in result.stdout.splitlines() if l.startswith("OK\t")][0]
    return Path(line.split("\t")[2])


def ask(root, title="Pick a backbone", question="Which backbone next?", priority="2", extra=(), session=A):
    return q(root, "ask", "--title", title, "--question", question, "--option", "A: small", "--option", "B: large",
             "--priority", priority, "--reason", "direction", "--session", session, *extra)


def git(root, *args):
    return subprocess.run(["git", "-C", str(root / "queue"), *args], capture_output=True, text=True, encoding="utf-8").stdout


def fresh(outer_git=False):
    root = Path(tempfile.mkdtemp())
    if outer_git:
        subprocess.run(["git", "init", "-q", str(root)], check=True)
    assert q(root, "init").returncode == 0
    return root


def board(root):
    return json.loads((root / "queue/board.json").read_text(encoding="utf-8"))


def test_init():
    root = Path(tempfile.mkdtemp())
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "src/queue").mkdir(parents=True)
    (root / "src/queue/mod.py").write_text("x = 1\n", encoding="utf-8")
    assert q(root, "init").returncode == 0
    assert (root / "queue/.git").is_dir() and (root / "queue/tickets").is_dir() and (root / "queue/board.json").is_file()
    assert len(git(root, "log", "--oneline").splitlines()) == 1
    assert "queue/" in (root / ".git/info/exclude").read_text(encoding="utf-8")
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "?? queue/" not in status.splitlines() and "?? src/" in status.splitlines(), status  # only the top queue/ is hidden
    again = q(root, "init")
    assert again.returncode == 0 and len(git(root, "log", "--oneline").splitlines()) == 1
    return root


def test_ask_decide():
    root = fresh()
    ticket = path_of(ask(root))
    assert ticket.name == f"{TODAY}-pick-a-backbone.md"
    text = ticket.read_text(encoding="utf-8")
    assert "status: needs-owner" in text and "waiting_on: owner" in text
    path_of(q(root, "decide", ticket.stem, "--text", "A, keep it small"))
    text = ticket.read_text(encoding="utf-8")
    assert f"decision: {TODAY} A, keep it small" in text
    assert "status: ready" in text and "waiting_on: agent" in text


def test_no_reask():
    root = fresh()
    ticket = path_of(ask(root))
    path_of(q(root, "decide", ticket.stem, "--text", "A"))
    again = ask(root, extra=("--ticket", ticket.stem))
    assert again.returncode == 1 and again.stdout.startswith("REFUSED"), again.stdout
    other = ask(root, question="A different question?", extra=("--ticket", ticket.stem))
    assert other.returncode == 0, other.stdout


def test_third_return():
    root = fresh()
    ticket = path_of(ask(root))
    path_of(q(root, "decide", ticket.stem, "--text", "A"))
    for _ in range(3):
        path_of(q(root, "move", ticket.stem, "doing"))
        path_of(q(root, "move", ticket.stem, "ready"))
    text = ticket.read_text(encoding="utf-8")
    assert "returns: 3" in text, text
    assert CLOSE_OR_SHRINK in text and "status: needs-owner" in text
    path_of(q(root, "move", ticket.stem, "needs-owner", "--priority", "1"))
    assert "returns: 3" in ticket.read_text(encoding="utf-8") and "priority: 1" in ticket.read_text(encoding="utf-8")


def test_parallel():
    root = fresh()
    cmd = lambda t: [sys.executable, "-I", "-X", "utf8", str(SCRIPT), "ask", str(root), "--title", t, "--question", "Q?",
                     "--option", "A", "--priority", "3", "--reason", "r", "--session", A]
    procs = [subprocess.Popen(cmd(t), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=ENV)
             for t in ("first one", "second one")]
    outs = [p.communicate() for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    names = [f"tickets/{TODAY}-first-one.md", f"tickets/{TODAY}-second-one.md"]
    for mine, other in (names, names[::-1]):
        sha = git(root, "log", "--format=%H", "--diff-filter=A", "--", mine).split()[-1]
        files = git(root, "show", "--name-only", "--format=", sha).split()
        assert mine in files and other not in files, (mine, files)
        assert set(files) <= {mine, "board.json", "inbox.html"}, files


def test_board():
    root = fresh()
    (root / "notes").mkdir()
    (root / "notes/ledger.md").write_text("step 1 done\nstep 3 done\n\n", encoding="utf-8")
    (root / "notes/long.md").write_text("x" * 200 + "\n", encoding="utf-8")
    a = path_of(ask(root, title="linked", extra=("--link", "notes\\ledger.md")))
    b = path_of(ask(root, title="missing", extra=("--link", "notes/nope.md")))
    c = path_of(ask(root, title="long", extra=("--link", "notes/long.md")))
    for t in (a, b, c):
        path_of(q(root, "decide", t.stem, "--text", "go"))
        path_of(q(root, "move", t.stem, "doing"))
    assert "links: [\"notes/ledger.md\"]" in a.read_text(encoding="utf-8")
    rows = {t["title"]: t for t in board(root)["tickets"]}
    assert rows["linked"]["last_line"] == "step 3 done"
    assert rows["missing"]["last_line"] == ""
    assert rows["long"]["last_line"] == "x" * 120
    assert board(root)["counts"]["doing"] == 3


def test_order():
    root = fresh()
    path_of(ask(root, title="low", priority="3"))
    path_of(ask(root, title="high", priority="1"))
    assert [t["title"] for t in board(root)["tickets"]] == ["high", "low"]


def test_bad_ticket():
    root = fresh()
    path_of(ask(root, title="good"))
    (root / "queue/tickets/broken.md").write_text("no frontmatter here\n", encoding="utf-8")
    out = q(root, "board")
    assert out.returncode == 0, out.stdout + out.stderr
    assert re.search(r"^SKIPPED\t.*broken\.md\t", out.stdout, re.M), out.stdout
    assert [t["title"] for t in board(root)["tickets"]] == ["good"]


def test_bad_links():
    root = fresh()
    good = path_of(ask(root, title="good"))
    text = good.read_text(encoding="utf-8").replace("links: []", "links: 5")
    (root / "queue/tickets/links.md").write_text(text, encoding="utf-8")
    out = q(root, "board")
    assert out.returncode == 0 and "Traceback" not in out.stderr, out.stdout + out.stderr
    assert re.search(r"^SKIPPED\t.*links\.md\t", out.stdout, re.M), out.stdout


def test_newlines():
    root = fresh()
    for extra in (["--reason", "a\nb"], ["--title", "a\nb"], ["--option", "x\ndecision: y"], ["--session", "s\nstatus: done"]):
        args = {"--title": "t", "--question": "q", "--option": "A", "--priority": "2", "--reason": "r", "--session": A}
        args[extra[0]] = extra[1]
        out = q(root, "ask", *[x for kv in args.items() for x in kv])
        assert out.stdout.startswith("REFUSED\t"), (extra, out.stdout + out.stderr)
    assert not list((root / "queue/tickets").glob("*.md"))
    one = path_of(ask(root, title="one", question="first line\n\nsecond line"))
    out = q(root, "decide", one.stem, "--text", "go\ndecision: other")
    assert out.stdout.startswith("REFUSED\t"), out.stdout
    assert "decision: " not in one.read_text(encoding="utf-8")


def test_ids():
    root = fresh()
    korean = path_of(ask(root, title="산사태"))
    assert re.fullmatch(rf"{TODAY}-t\d+", korean.stem), korean.stem
    one = path_of(ask(root, title="Same title"))
    two = path_of(ask(root, title="Same title"))
    assert (one.stem, two.stem) == (f"{TODAY}-same-title", f"{TODAY}-same-title-2")


def test_atomic():
    root = fresh()
    path_of(ask(root))
    path_of(q(root, "board"))
    assert not list((root / "queue").glob("*.tmp"))


def test_inbox():
    root = fresh()
    (root / "log.md").write_text("halfway there\n", encoding="utf-8")
    path_of(ask(root, title="second <script>x</script>", question="Is this second?", priority="2"))
    path_of(ask(root, title="first", question="Is this first?", priority="1"))
    run = path_of(ask(root, title="running", extra=("--link", "log.md")))
    path_of(q(root, "decide", run.stem, "--text", "go"))
    path_of(q(root, "move", run.stem, "doing"))
    html = (root / "queue/inbox.html").read_text(encoding="utf-8")
    assert html.index("first") < html.index("second &lt;script&gt;"), html
    assert "<script>" not in html
    assert "Is this first?" in html and "Is this second?" in html and "A: small" in html
    assert html.index("In progress") < html.index("halfway there")
    assert "http://" not in html and "https://" not in html
    assert "<title>Work queue</title>" in html
    assert "Core documents" not in html, "no docs/core, no section"


def test_core_docs():
    root = fresh()
    core = root / "docs/core"
    (core / "features").mkdir(parents=True)
    (core / "goals.md").write_text("# Goals\n\n## Final goal\n\n> Ship five <b>stable</b> APIs.\n\n## Now\n\n- billing first\n", encoding="utf-8")
    (core / "features/app.md").write_text("# App\n\nThe entry point is `src/app.py`.\n\n## Flow\n\ntext\n\n## Traps\n\ntext\n", encoding="utf-8")
    (core / "core.json").write_text(json.dumps({"drift": "billing has no recent work", "reviewed": {"goals.md": time.time() - 3 * 86400},
                                                "docs": [{"file": "goals.md", "brief": "what we aim at"},
                                                         {"file": "features/app.md", "brief": "the app"},
                                                         {"file": "gone.md", "brief": "deleted file"}]}), encoding="utf-8")
    assert q(root, "board").returncode == 0
    html = (root / "queue/inbox.html").read_text(encoding="utf-8")
    sec = html[html.index("Core documents"):]
    for s in ("Ship five &lt;b&gt;stable&lt;/b&gt; APIs.", "billing has no recent work", "goals.md", "what we aim at", "Final goal · Now",
              "features/app.md", "The entry point is `src/app.py`.", "Flow · Traps", "3 d", "never reviewed", "gone.md", "missing"):
        assert s in sec, s
    assert "<b>stable</b>" not in html


def test_session_ids():
    root = fresh()
    t = path_of(ask(root, session=f"local_{A.upper()}"))
    assert f"session: {A}" in t.read_text(encoding="utf-8")
    for bad in ("abc", A[:8], f"{A}x", ""):
        out = ask(root, title="bad", session=bad)
        assert out.stdout.startswith("REFUSED\t") and "UUID" in out.stdout, (bad, out.stdout)
    path_of(q(root, "move", t.stem, "needs-owner", "--session", f"local_{B}"))
    assert f"session: {B}" in t.read_text(encoding="utf-8")
    assert q(root, "move", t.stem, "needs-owner", "--session", "zz").stdout.startswith("REFUSED\t")


def test_hook_routing():
    root = fresh()
    queue_py = SCRIPT.as_posix()
    start = hook(root, A, "SessionStart")
    assert f"--session {A}" in start and queue_py in start, start
    commands = [c.split(" <")[0] for c in re.findall(r"`([^`]*queue\.py[^`]*)`", start)]
    assert len(commands) == 4, start
    for c in commands:  # each command the rule gives must parse as written
        ran = subprocess.run(shlex.split(c) + ["--help"], capture_output=True, text=True, encoding="utf-8", env=ENV)
        assert ran.returncode == 0 and "usage:" in ran.stdout, c + ran.stderr
    t = path_of(ask(root, title="Pick a backbone"))
    assert t.stem in hook(root, A, "SessionStart"), "the owner's open tickets are listed at start"
    other = hook(root, B, "UserPromptSubmit", f"{t.stem}: take A")
    assert f"Ticket {t.stem} belongs to session {A}" in other and f"decide \"{root.resolve().as_posix()}\" {t.stem}" in other, other
    assert f"deliver \"{root.resolve().as_posix()}\" {t.stem}" in other and "Do not do that ticket's work here" in other, other
    assert hook(root, B, "UserPromptSubmit", "take A") == "", "no id named: nothing"
    assert hook(root, B, "UserPromptSubmit", "20990101-made-up take A") == "", "an id not on the board: nothing"
    assert hook(root, A, "UserPromptSubmit", f"{t.stem}: take A") == "", "the owner's own ticket, still waiting: nothing"
    path_of(q(root, "decide", t.stem, "--text", "A"))
    pulled = hook(root, A, "UserPromptSubmit", "hi")
    assert f"Ticket {t.stem} was decided for you: read {t.resolve().as_posix()}" in pulled and f"move" in pulled, pulled
    assert "decided for you" in hook(root, A, "SessionStart")
    assert "decided for you" not in hook(root, B, "UserPromptSubmit", "hi"), "B never gets A's pull"
    path_of(q(root, "move", t.stem, "doing"))
    assert hook(root, A, "UserPromptSubmit", "hi") == ""


def test_deliver():
    root = fresh()
    t = path_of(ask(root))
    assert q(root, "deliver", t.stem).stdout.startswith("REFUSED\tnot decided"), "nothing to deliver before a decision"
    path_of(q(root, "decide", t.stem, "--text", "A"))
    out = q(root, "deliver", t.stem)
    assert out.returncode == 0 and out.stdout == f"SEND\tlocal_{A}\tticket {t.stem} decided, read the ticket file {t.resolve().as_posix()}\n", out.stdout
    old = t.read_text(encoding="utf-8").replace(f"session: {A}", "session: s1")
    t.write_text(old, encoding="utf-8")
    assert q(root, "deliver", t.stem).stdout.startswith("REFUSED\t"), "an old free-form session value is not sent to"


def test_sessions_repair():
    root = fresh()
    transcript(root, A)
    transcript(root, B)
    transcript(root, "aaaaaaaa-9999-4999-8999-999999999999")  # shares A's 8-character prefix
    made = [path_of(ask(root, title=f"t{i}")) for i in range(4)]
    for path, value in zip(made, (f"local_{B}", B[:8], "aaaaaaaa", "s1")):
        path.write_text(path.read_text(encoding="utf-8").replace(f"session: {A}", f"session: {value}"), encoding="utf-8")
    out = q(root, "sessions")
    assert out.returncode == 0 and "OK\tsessions" in out.stdout and "2 fixed" in out.stdout, out.stdout
    assert f"session: {B}" in made[0].read_text(encoding="utf-8") and f"session: {B}" in made[1].read_text(encoding="utf-8")
    assert "session: aaaaaaaa\n" in made[2].read_text(encoding="utf-8") and re.search(rf"LEFT\t{made[2].stem}\taaaaaaaa\t2 ", out.stdout)
    assert "session: s1\n" in made[3].read_text(encoding="utf-8") and f"LEFT\t{made[3].stem}\ts1\t0 " in out.stdout
    assert "sessions 2" in git(root, "log", "-1", "--format=%s")


def test_hook_silent():
    root = fresh()
    bare = Path(tempfile.mkdtemp())
    assert hook(bare, A, "SessionStart") == "", "no queue"
    assert hook(root, "not-a-uuid", "SessionStart") == "", "no session id"
    assert hook(root, A, "SessionStart", stdin="{broken") == "", "broken stdin"
    (root / "queue/board.json").write_text("{broken", encoding="utf-8")
    assert hook(root, A, "SessionStart") == "", "broken board"


def test_hook_cap():
    root = fresh()
    for i in range(11):
        t = path_of(ask(root, title=f"t{i}"))
        path_of(q(root, "decide", t.stem, "--text", "go"))
    out = hook(root, A, "UserPromptSubmit", "hi")
    assert len(out.splitlines()) == 10 and all("decided for you" in l for l in out.splitlines()), out


def test_can_wait():
    root = fresh()
    wait = path_of(ask(root, title="can wait", extra=("--default", "keep A and go on")))
    block = path_of(ask(root, title="blocking"))
    rows = {t["title"]: t for t in board(root)["tickets"]}
    assert rows["can wait"]["can_wait"] and rows["can wait"]["default"] == "keep A and go on" and not rows["blocking"]["can_wait"]
    html = (root / "queue/inbox.html").read_text(encoding="utf-8")
    needs, later = html.index("Needs your answer (1)"), html.index("Can wait (1)")
    assert needs < html.index("blocking") < later < html.index("can wait") and "keep A and go on" in html, html
    path_of(q(root, "decide", wait.stem, "--text", "self: kept A"))
    assert "status: done" in wait.read_text(encoding="utf-8"), "a self decision closes the ticket"
    assert "Can wait" not in (root / "queue/inbox.html").read_text(encoding="utf-8")
    path_of(q(root, "done", block.stem))
    assert "Needs your answer (0)" in (root / "queue/inbox.html").read_text(encoding="utf-8")


def test_board_sessions():
    root = fresh()
    gone = "cccccccc-3333-4333-8333-333333333333"
    t_a = path_of(ask(root, title="for a"))
    t_b = path_of(ask(root, title="for b", session=B))
    path_of(ask(root, title="for gone", session=gone))
    asked = t_a.stat().st_mtime
    transcript(root, A, title="Fire model work", mtime=asked + 900)  # wrote well after the ask
    transcript(root, B, mtime=t_b.stat().st_mtime - 10)  # stopped right after asking, no title
    data = json.loads(q(root, "board").stdout and (root / "queue/board.json").read_text(encoding="utf-8"))
    rows = {t["title"]: t for t in data["tickets"]}
    assert data["sessions"][A]["title"] == "Fire model work", "the last custom-title, read from the tail"
    assert data["sessions"][B]["title"] == B[:8], "no title in the tail: the short id"
    assert rows["for a"]["still_working"] and not rows["for b"]["still_working"]
    assert data["sessions"][gone]["gone"] and data["attention"]["gone"] == 1
    assert data["inbox"].endswith(":inbox")
    html = (root / "queue/inbox.html").read_text(encoding="utf-8")
    assert "Fire model work" in html and "still working" in html and "stopped" in html
    assert not re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", html), "no full session id on the page"
    path_of(q(root, "decide", t_b.stem, "--text", "go"))
    os.utime(t_b, (time.time() - 2 * 86400,) * 2)
    data = json.loads((q(root, "board"), (root / "queue/board.json").read_text(encoding="utf-8"))[1])
    assert data["attention"]["stale_ready"] == 1
    assert "Decided, not picked up (1)" in (root / "queue/inbox.html").read_text(encoding="utf-8")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("pass")


if __name__ == "__main__":
    main()
