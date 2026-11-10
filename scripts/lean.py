#!/usr/bin/env python3
"""lean: inject the working-style rules from skills/lean/SKILL.md at session and subagent start. Standard library only.

  lean.py hook    read a Claude Code hook event on stdin, print what it should inject

UserPromptSubmit "phro-lean off|on" (the whole prompt) switches the current session; the marker is <config>/phro-lean/off/<session_id>.

SessionStart gets Build, Answer and Switch; SubagentStart gets Build and Report. Nothing is injected while the
original ponytail or i-have-adhd plugin is enabled, so the two never stack. A hook never blocks: any failure prints nothing.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SKILL = PLUGIN / "skills/lean/SKILL.md"
MAIN = ["Build", "Answer", "Switch"]
SUB = ["Build", "Report"]
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
    if name not in ("SessionStart", "SubagentStart") or is_off(sid) or originals_enabled(event.get("cwd")):
        return ""
    if name == "SessionStart":
        return main_text()
    ctx = sections(SKILL.read_text(encoding="utf-8"), SUB)
    return json.dumps({"hookSpecificOutput": {"hookEventName": "SubagentStart", "additionalContext": ctx}})


def main(argv):
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
