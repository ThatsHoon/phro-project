#!/usr/bin/env python3
"""The weekday morning report of a project's copy: yesterday's finished work and today's work, in docs/daily. Standard library only.

  daily.py hook                                   SessionStart / UserPromptSubmit: start `run` in the background when today's report is due
  daily.py run [--date YYYY-MM-DD] [--force]      gather the period, ask `claude -p` for the report, check its form, write the file
  daily.py digest [--date YYYY-MM-DD]             print what `run` would send, for a look

Only a copy writes reports (origin.json names its project); the original plugin stays silent. Times are KST.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
HOUR = 8
DAYS = "월화수목금토일"
CHILD = "PHRO_DAILY_CHILD"  # set for the background run and the model call, so neither starts another run
RETRY_S = 30 * 60
PER_SESSION = 2400  # characters of one session in the digest
DIGEST = 60000
TAG = re.compile(r"<([a-zA-Z][\w-]*)[^>]*>.*?</\1>", re.S)
STAMP = re.compile(rb'"timestamp":\s*"(\d{4}-\d\d-\d\dT[\d:.]+Z?)"')
SPOKEN = re.compile(rb'"type":\s*"(?:user|assistant)"')


def project():
    try:
        return Path(json.loads((HERE / "origin.json").read_text(encoding="utf-8"))["project"])
    except (OSError, ValueError, KeyError):
        return None


def out_dir():
    return HERE / "docs" / "daily"


def name_of(day):
    return f"{day:%Y-%m-%d}({DAYS[day.weekday()]}).md"


def period(day):
    """[previous weekday 00:00, day 00:00) in KST: Monday covers Friday to Sunday."""
    end = datetime(day.year, day.month, day.day, tzinfo=KST)
    start = end - timedelta(days=1)
    while start.weekday() >= 5:
        start -= timedelta(days=1)
    return start, end


def due(now):
    """Today's date when its report should exist by now, else None. lean: no public holidays | when: owner | fix: a holiday list in docs/daily"""
    now = now.astimezone(KST)
    if now.weekday() >= 5 or now.hour < HOUR:
        return None
    return now.date()


def stamp(raw):
    m = STAMP.search(raw[:4000]) or STAMP.search(raw)
    if not m:
        return None
    try:
        return datetime.fromisoformat(m.group(1).decode().replace("Z", "+00:00"))
    except ValueError:
        return None


def entries(path, start, end, chunk=4 << 20):
    """Transcript lines with a timestamp in [start, end), read backwards from the end until one is older than start."""
    found = []
    with open(path, "rb") as f:
        pos, rest = f.seek(0, 2), b""
        while True:
            n = min(chunk, pos)
            pos -= n
            f.seek(pos)
            parts = (f.read(n) + rest).split(b"\n")
            rest = parts.pop(0) if pos else b""
            for raw in reversed(parts):  # at pos 0 nothing was popped, so the first line is in parts
                t = stamp(raw) if raw.strip() else None
                if t is None:
                    continue
                if t < start:
                    return found[::-1]
                if t < end:
                    found.append(raw)
            if not pos:
                return found[::-1]
            chunk = min(chunk * 2, 64 << 20)


def text_of(content):
    if isinstance(content, str):
        parts = [content]
    else:
        parts = [c.get("text", "") for c in content or [] if isinstance(c, dict) and c.get("type") == "text"]
    return " ".join(TAG.sub("", p).strip() for p in parts).strip()


def title_of(path):
    with open(path, "rb") as f:
        f.seek(max(0, f.seek(0, 2) - (64 << 10)))
        tail = f.read().decode("utf-8", "replace")
    titles = re.findall(r'"customTitle":\s*"((?:[^"\\]|\\.)*)"', tail)
    return json.loads(f'"{titles[-1]}"') if titles else path.stem[:8]


def session(path, start, end):
    """One session's typed prompts and last replies inside the period, or None."""
    asked, said = [], []
    for raw in entries(path, start, end):
        if not SPOKEN.search(raw):
            continue
        try:
            d = json.loads(raw)
        except ValueError:
            continue
        if d.get("isSidechain") or d.get("isMeta"):
            continue
        text = text_of((d.get("message") or {}).get("content"))
        if not text or text.startswith(("Caveat:", "[Request interrupted")):
            continue
        (asked if d.get("type") == "user" else said).append(text)
    if not asked and not said:
        return None
    lines = [f"## {title_of(path)}", *(f"- asked: {a[:240]}" for a in asked[-12:]), *(f"- replied: {s[:700]}" for s in said[-3:])]
    return "\n".join(lines)[:PER_SESSION]


def transcript_files(root, start):
    base = Path.home() / ".claude/projects"
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve()))
    folders = [p for p in base.glob(f"{slug}*") if p.is_dir() and (p.name == slug or p.name.startswith(slug + "-"))]
    files = [f for d in folders for f in d.glob("*.jsonl") if f.stat().st_mtime >= start.timestamp()]
    return sorted(files, key=lambda f: f.stat().st_mtime)


def commits(root, start, end):
    r = subprocess.run(["git", "-C", str(root), "log", "--all", "--no-merges", f"--since={start.isoformat()}", f"--until={end.isoformat()}",
                        "--format=%s"], capture_output=True, text=True, encoding="utf-8")
    return [l for l in r.stdout.splitlines() if l.strip()][:40] if r.returncode == 0 else []


def tickets(root, start, end):
    try:
        board = json.loads((Path(root) / "queue/board.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return [], []
    closed, open_ = [], []
    for t in board.get("tickets", []):
        changed = board.get("generated", 0) - t.get("changed_s", 0)
        if t.get("status") == "done" and start.timestamp() <= changed < end.timestamp():
            closed.append(t["title"])
        elif t.get("status") != "done":
            open_.append(f"{t['title']} ({t['status']})")
    return closed, open_


def goals(root):
    try:
        text = (Path(root) / "docs/core/README.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"^## 지금 목표\s*\n(.*?)(?=^## |\Z)", text, re.S | re.M)
    return m.group(1).strip()[:1500] if m else ""


def digest(root, day):
    start, end = period(day)
    parts = [f"# Period: {start:%Y-%m-%d %H:%M} to {end:%Y-%m-%d %H:%M} KST, project {Path(root).name}"]
    sessions = [s for s in (session(f, start, end) for f in transcript_files(root, start)) if s]
    parts += ["# Sessions (titles, what the owner asked, the last replies)", *sessions] if sessions else ["# Sessions: none in the period"]
    found = commits(root, start, end)
    if found:
        parts += ["# Commits", *(f"- {c}" for c in found)]
    closed, open_ = tickets(root, start, end)
    if closed:
        parts += ["# Queue tickets closed", *(f"- {c}" for c in closed)]
    if open_:
        parts += ["# Queue tickets still open", *(f"- {o}" for o in open_[:20])]
    g = goals(root)
    if g:
        parts += ["# Current goals (docs/core)", g]
    return "\n".join(parts)[:DIGEST], bool(sessions or found or closed)


def style():
    text = (HERE / "skills/daily/SKILL.md").read_text(encoding="utf-8")
    return text[text.index("## Style"):].strip()


HEADS = ["어제 완료:", "오늘 할 일:"]
ITEM_MAX = 42  # "* " plus 40 characters

PROMPT = """Write a Korean daily work report from the material below. Output only the report, nothing before or after it.
Exactly two sections, each heading once: `어제 완료:` (work finished or clearly advanced in the period) and `오늘 할 일:` (the next steps the material names: open questions, open tickets, the last replies' next actions, the current goals). Merge the same work across sessions into one item per piece of work. The reader is a teammate who never saw these sessions: say what was done in everyday words, the way a person posts it in the team chat. When the period had no work, write `* 작업 없음` under 어제 완료.

{style}

# Material
{material}
"""


def check(text):
    """The report's lines, or ValueError naming the first line that breaks the form."""
    lines = [l.rstrip() for l in text.strip().strip("`").strip().splitlines()]
    if not lines or not lines[0].endswith(":"):
        raise ValueError(f"first line is not a heading: {lines[:1]}")
    heads, count = [], {}
    for l in lines:
        if l and not (l.startswith("* ") or (l.endswith(":") and not l.startswith(("-", "#")))):
            raise ValueError(f"not a heading or a `* ` item: {l[:80]}")
        if l.endswith(":") and not l.startswith("* "):
            heads.append(l)
        elif l:
            if re.search(r"[다요]\.?$", l):
                raise ValueError(f"item ends as a sentence: {l[:80]}")
            if len(l) > ITEM_MAX:  # long items are where the jargon goes
                raise ValueError(f"item longer than {ITEM_MAX - 2} characters: {l[:80]}")
            count[heads[-1]] = count.get(heads[-1], 0) + 1
    if heads != HEADS:
        raise ValueError(f"headings must be exactly {HEADS}, got {heads}")
    if any(n > 5 for n in count.values()):
        raise ValueError(f"more than 5 items under one heading: {count}")
    return "\n".join(lines) + "\n"


def claude_cmd():
    """The model command: `claude`, or PHRO_DAILY_CLAUDE (a JSON list or one program), which tests point at a stub."""
    v = os.environ.get("PHRO_DAILY_CLAUDE", "claude")
    return json.loads(v) if v.startswith("[") else [shutil.which(v) or v]


def ask_model(prompt):
    env = {**os.environ, CHILD: "1"}
    with tempfile.TemporaryDirectory() as cwd:  # the call leaves no transcript in the project
        r = subprocess.run([*claude_cmd(), "-p", "--model", "sonnet", "--tools", "", "--no-session-persistence"], input=prompt, cwd=cwd,
                           capture_output=True, text=True, encoding="utf-8", env=env, timeout=600)
    if r.returncode:
        raise RuntimeError(f"claude -p failed ({r.returncode}): {r.stderr.strip()[:300]}")
    return r.stdout


def run(day, force=False):
    root = project()
    if root is None:
        print("SKIPPED\tthe original plugin writes no report")
        return 0
    target = out_dir() / name_of(day)
    if target.exists() and not force:
        print(f"EXISTS\t{target.as_posix()}")
        return 0
    state = out_dir() / ".state"
    state.mkdir(parents=True, exist_ok=True)
    try:
        material, _ = digest(root, day)
        prompt = PROMPT.format(style=style(), material=material)
        try:
            text = check(ask_model(prompt))
        except ValueError as e:  # one more try, told what was wrong
            text = check(ask_model(prompt + f"\n\nYour last answer broke the form ({e}). Follow the style exactly."))
        tmp = target.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        (state / f"{day}.error").unlink(missing_ok=True)
        print(f"OK\tdaily\t{target.as_posix()}")
        return 0
    except Exception as e:
        (state / f"{day}.error").write_text(f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{type(e).__name__}: {e}\n", encoding="utf-8")
        print(f"FAILED\t{type(e).__name__}: {e}")
        return 1
    finally:
        (state / f"{day}.lock").unlink(missing_ok=True)


def claim(day):
    """True when this process may start today's run: no report yet, no fresh lock, no fresh error."""
    if (out_dir() / name_of(day)).exists():
        return False
    state = out_dir() / ".state"
    state.mkdir(parents=True, exist_ok=True)
    for kind in ("lock", "error"):
        p = state / f"{day}.{kind}"
        if p.exists() and time.time() - p.stat().st_mtime < RETRY_S:
            return False
    lock = state / f"{day}.lock"
    lock.unlink(missing_ok=True)
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    os.write(fd, str(os.getpid()).encode())
    os.close(fd)
    return True


def start_background(day):
    argv = [sys.executable, "-I", "-X", "utf8", str(Path(__file__).resolve()), "run", "--date", str(day)]
    opts = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env={**os.environ, CHILD: "1"}, close_fds=True)
    if os.name == "nt":
        base = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        try:
            return subprocess.Popen(argv, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB, **opts)
        except OSError:  # the job forbids breakaway
            return subprocess.Popen(argv, creationflags=base, **opts)
    return subprocess.Popen(argv, start_new_session=True, **opts)


def hook():
    if os.environ.get(CHILD) or project() is None:
        return
    day = due(datetime.now(KST))
    if day and claim(day):
        start_background(day)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("hook")
    for name in ("run", "digest"):
        s = sub.add_parser(name)
        s.add_argument("--date", type=lambda v: datetime.strptime(v, "%Y-%m-%d").date(), default=datetime.now(KST).date())
        if name == "run":
            s.add_argument("--force", action="store_true")
    a = p.parse_args()
    if a.command == "hook":
        try:
            sys.stdin.read()
            hook()
        except Exception:  # a hook never breaks the session
            pass
        return 0
    if a.command == "digest":
        root = project()
        print(digest(root, a.date)[0] if root else "SKIPPED\tthe original plugin writes no report")
        return 0
    return run(a.date, a.force)


if __name__ == "__main__":
    sys.exit(main())
