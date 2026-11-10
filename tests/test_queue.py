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
    root = fresh(outer_git=True)
    assert (root / "queue/.git").is_dir() and (root / "queue/tickets").is_dir() and (root / "queue/board.json").is_file()
    assert len(git(root, "log", "--oneline").splitlines()) == 1
    assert "queue/" in (root / ".git/info/exclude").read_text(encoding="utf-8")
    status = subprocess.run(["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True).stdout
    assert "queue" not in status, status
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


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print("pass")


if __name__ == "__main__":
    main()
