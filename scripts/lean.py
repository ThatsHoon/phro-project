#!/usr/bin/env python3
"""lean: inject the working-style rules from skills/lean/SKILL.md at session and subagent start. Standard library only.

  lean.py hook    read a Claude Code hook event on stdin, print what it should inject
  lean.py stats <transcripts_dir> [--days N]
                  per arm (lean injected or not): sessions, replies, and the share of replies with a list group over
                  5 items or more than one question in the last paragraph. Counts only, never text.

UserPromptSubmit "phro-lean off|on" (the whole prompt) switches the current session; the marker is <config>/phro-lean/off/<session_id>.

SessionStart gets Build, Answer and Switch, except on a resume that already holds them unchanged. SubagentStart
gets Build and Report, and Report alone for the read-only built-in agents. Nothing is injected while the
original ponytail or i-have-adhd plugin is enabled, so the two never stack. A hook never blocks: any failure prints nothing.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import once

PLUGIN = Path(__file__).resolve().parents[1]
SKILL = PLUGIN / "skills/lean/SKILL.md"
MAIN = ["Build", "Answer", "Switch"]
SUB = ["Build", "Report"]
READ_ONLY = {"Explore", "Plan", "claude-code-guide", "web-fetch"}
LIST_ITEM = re.compile(r"(?:[-*+]|\d+[.)])\s")
HEADER = "PHRO-LEAN ON. The rules below apply to every response in this session."
ORIGINALS = ("ponytail@", "i-have-adhd@")
TOKEN = re.compile(r"[A-Za-z0-9_-]+")
SWITCH = re.compile(r"\s*/?(?:phro-lean|phro-project[\w.-]*:lean)\s+(on|off)\s*\Z", re.I)
OFF_TEXT = 'PHRO-LEAN OFF for this session: stop applying the lean rules from here on. "phro-lean on" brings them back.'
GUARD_NOTE = " (ponytail or i-have-adhd is still enabled, so lean injects nothing until they are disabled.)"


def config_dir():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def sections(text, names):
    """The named `## ` sections, in the order asked, each with its heading; frontmatter and CRLF do not matter."""
    text = re.sub(r"\A---\n.*?\n---\n", "", text.replace("\r\n", "\n"), flags=re.S)
    found = {}
    for part in re.split(r"(?m)^(?=## )", text):
        if part.startswith("## "):
            found[part[3:].split("\n", 1)[0].strip()] = part.strip()
    return "\n\n".join(found[n] for n in names if n in found)


def originals_enabled(cwd):
    """True when a later settings file leaves ponytail or i-have-adhd enabled. Unreadable files count as empty."""
    plugins = {}
    for path in (config_dir() / "settings.json", Path(cwd or ".") / ".claude/settings.json",
                 Path(cwd or ".") / ".claude/settings.local.json"):
        try:
            plugins.update(json.loads(path.read_text(encoding="utf-8-sig")).get("enabledPlugins") or {})
        except (OSError, ValueError, AttributeError):
            pass
    return any(v is True and k.startswith(ORIGINALS) for k, v in plugins.items())


def off_file(session_id):
    return config_dir() / "phro-lean/off" / session_id if session_id and TOKEN.fullmatch(session_id) else None


def is_off(session_id):
    path = off_file(session_id)
    return bool(path and path.exists())


def set_off(session_id, off):
    """Marks this session off or on; False for an id that is not a plain token, which then stays on."""
    path = off_file(session_id)
    if not path:
        return False
    if off:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    else:
        path.unlink(missing_ok=True)
    return True


def prune(days=7):
    """Drops off-markers older than `days`; only session-named files, nothing else in the folder."""
    folder = config_dir() / "phro-lean/off"
    cutoff = time.time() - days * 86400
    for path in folder.glob("*") if folder.is_dir() else []:
        if TOKEN.fullmatch(path.name) and path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)


def main_text():
    return HEADER + "\n\n" + sections(SKILL.read_text(encoding="utf-8"), MAIN)


def switch(event):
    m = SWITCH.match(event.get("prompt") or "")
    if not m:
        return ""
    off = m.group(1).lower() == "off"
    set_off(event.get("session_id") or "", off)
    note = GUARD_NOTE if originals_enabled(event.get("cwd")) else ""
    return (OFF_TEXT if off else main_text()) + note


def handle(event):
    name, sid = event.get("hook_event_name"), event.get("session_id") or ""
    if name == "UserPromptSubmit":
        return switch(event)
    if name == "SessionStart" and event.get("source") == "startup":
        prune()
        once.prune()
    if name not in ("SessionStart", "SubagentStart") or is_off(sid) or originals_enabled(event.get("cwd")):
        return ""
    if name == "SessionStart":
        text = main_text()
        return text if once.needed(sid, "lean", text, event.get("source")) else ""
    names = ["Report"] if event.get("agent_type") in READ_ONLY else SUB
    ctx = sections(SKILL.read_text(encoding="utf-8"), names)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ctx}})


def reply_flags(text):
    """(a list group of more than 5 top-level items, more than one question in the last paragraph)."""
    run = longest = 0
    for line in text.splitlines():
        run = run + 1 if LIST_ITEM.match(line) else 0
        longest = max(longest, run)
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    last = paragraphs[-1] if paragraphs else ""
    return longest > 5, last.count("?") + last.count("？") > 1


def stats(folder, days):
    """Rows per arm: sessions, replies, big_list, multi_question. Reads assistant text and hook attachments only."""
    cutoff = time.time() - days * 86400
    arms = {True: [0, 0, 0, 0], False: [0, 0, 0, 0]}
    for path in sorted(Path(folder).glob("*.jsonl")):
        if path.stat().st_mtime < cutoff:
            continue
        on, rows = False, []
        with path.open(encoding="utf-8", errors="replace") as f:
            for raw in f:
                if '"attachment"' in raw and HEADER in raw:
                    on = True
                if '"assistant"' not in raw:
                    continue
                try:
                    entry = json.loads(raw)
                except ValueError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                content = (entry.get("message") or {}).get("content")
                text = "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text") if isinstance(content, list) else ""
                if text.strip():
                    rows.append(reply_flags(text))
        if rows:
            arm = arms[on]
            arm[0] += 1
            arm[1] += len(rows)
            arm[2] += sum(r[0] for r in rows)
            arm[3] += sum(r[1] for r in rows)
    out = ["arm\tsessions\treplies\tbig_list\tmulti_question"]
    for on in (True, False):
        s, n, big, multi = arms[on]
        pct = (lambda k: f"{100 * k / n:.1f}%") if n else (lambda k: "-")
        out.append(f"{'lean' if on else 'none'}\t{s}\t{n}\t{pct(big)}\t{pct(multi)}")
    return out


def main(argv):
    if argv[1:2] == ["stats"] and len(argv) >= 3:
        days = float(argv[argv.index("--days") + 1]) if "--days" in argv else 7.0
        print("\n".join(stats(argv[2], days)))
        return 0
    if argv[1:] != ["hook"]:
        print(__doc__)
        return 0
    try:
        out = handle(json.loads(sys.stdin.read() or "{}"))
    except Exception:  # a hook must never break a session
        return 0
    if out:
        print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
