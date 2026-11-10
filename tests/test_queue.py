"""The work queue: tickets in their own git repo, one-path commits, no re-asking, the third return, the board.

Run: python -I -X utf8 tests/test_queue.py
"""
import json
import re
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


def q(root, *args):
    return subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), args[0], str(root), *args[1:]],
                          capture_output=True, text=True, encoding="utf-8")


def path_of(result):
    assert result.returncode == 0, result.stdout + result.stderr
    line = [l for l in result.stdout.splitlines() if l.startswith("OK\t")][0]
    return Path(line.split("\t")[2])


def ask(root, title="Pick a backbone", question="Which backbone next?", priority="2", extra=()):
    return q(root, "ask", "--title", title, "--question", question, "--option", "A: small", "--option", "B: large",
             "--priority", priority, "--reason", "direction", "--session", "s1", *extra)


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
                     "--option", "A", "--priority", "3", "--reason", "r", "--session", "s"]
    procs = [subprocess.Popen(cmd(t), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
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
        args = {"--title": "t", "--question": "q", "--option": "A", "--priority": "2", "--reason": "r", "--session": "s"}
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
    assert html.index("Running") < html.index("halfway there")
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


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("pass")


if __name__ == "__main__":
    main()
