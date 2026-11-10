#!/usr/bin/env python3
"""lean: inject the working-style rules from skills/lean/SKILL.md at session and subagent start. Standard library only.

  lean.py hook    read a Claude Code hook event on stdin, print what it should inject

SessionStart gets Build, Answer and Switch; SubagentStart gets Build and Report. Nothing is injected while the
original ponytail or i-have-adhd plugin is enabled, so the two never stack. A hook never blocks: any failure prints nothing.
"""
import json
import os
import re
import sys
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
SKILL = PLUGIN / "skills/lean/SKILL.md"
MAIN = ["Build", "Answer", "Switch"]
SUB = ["Build", "Report"]
HEADER = "PHRO-LEAN ON. The rules below apply to every response in this session."
ORIGINALS = ("ponytail@", "i-have-adhd@")
TOKEN = re.compile(r"[A-Za-z0-9_-]+")


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


def main_text():
    return HEADER + "\n\n" + sections(SKILL.read_text(encoding="utf-8"), MAIN)


def handle(event):
    name, sid = event.get("hook_event_name"), event.get("session_id") or ""
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
