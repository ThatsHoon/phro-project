"""lean hook: what each event injects, the size limits, the guard against the original plugins, and the fixed rules in the skill texts.

Run: python -I -X utf8 tests/test_lean.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/lean.py"
SKILL = ROOT / "skills/lean/SKILL.md"
sys.path.insert(0, str(SCRIPT.parent))


def run(event, config, cwd):
    """Runs the hook as Claude Code does; returns (exit code, stdout)."""
    env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config))
    data = event if isinstance(event, str) else json.dumps(event)
    res = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "hook"], input=data, capture_output=True,
                         text=True, encoding="utf-8", env=env)
    return res.returncode, res.stdout


def write(path, text, bom=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(("﻿" if bom else "") + text, encoding="utf-8")


def start(sid, cwd, source="startup"):
    return {"hook_event_name": "SessionStart", "source": source, "session_id": sid, "cwd": str(cwd)}


def sub(sid, cwd):
    event = {"hook_event_name": "SubagentStart", "cwd": str(cwd)}
    if sid is not None:
        event["session_id"] = sid
    return event


def fresh():
    base = Path(tempfile.mkdtemp())
    (base / "config").mkdir()
    (base / "proj").mkdir()
    return base / "config", base / "proj"


def session_start():
    import lean
    config, cwd = fresh()
    code, out = run(start("s1", cwd), config, cwd)
    assert code == 0 and out.startswith(lean.HEADER), out[:200]
    for h in ("## Build", "## Answer", "## Switch"):
        assert h in out, h
    assert "## Report" not in out and "name: lean" not in out, out[:300]
    assert len(out.encode()) <= 6000, len(out.encode())


def subagent():
    config, cwd = fresh()
    code, out = run(sub("s1", cwd), config, cwd)
    data = json.loads(out)["hookSpecificOutput"]
    ctx = data["additionalContext"]
    assert data["hookEventName"] == "SubagentStart"
    assert "## Build" in ctx and "## Report" in ctx and "## Answer" not in ctx, ctx[:300]
    assert len(ctx.encode()) <= 4000, len(ctx.encode())


def subagent_no_id():
    config, cwd = fresh()
    _, out = run(sub(None, cwd), config, cwd)
    assert "## Build" in json.loads(out)["hookSpecificOutput"]["additionalContext"]


def guard():
    config, cwd = fresh()
    write(config / "settings.json", json.dumps({"enabledPlugins": {"ponytail@ponytail": True}}))
    assert run(start("s1", cwd), config, cwd)[1] == ""
    assert run(sub("s1", cwd), config, cwd)[1] == ""
    write(cwd / ".claude/settings.local.json", json.dumps({"enabledPlugins": {"ponytail@ponytail": False}}))
    assert "## Build" in run(start("s1", cwd), config, cwd)[1]


def bad_settings():
    config, cwd = fresh()
    write(config / "settings.json", json.dumps({"enabledPlugins": {"i-have-adhd@i-have-adhd": True}}), bom=True)
    assert run(start("s1", cwd), config, cwd)[1] == "", "a BOM settings file is still read"
    write(config / "settings.json", "{not json")
    assert "## Build" in run(start("s1", cwd), config, cwd)[1], "broken settings do not block"


def crlf():
    import lean
    text = SKILL.read_text(encoding="utf-8")
    assert lean.sections(text.replace("\n", "\r\n"), lean.MAIN) == lean.sections(text, lean.MAIN)


def broken_stdin():
    config, cwd = fresh()
    assert run("{", config, cwd) == (0, "")


def fixed_rules():
    text = SKILL.read_text(encoding="utf-8")
    for s in ("Do not re-append a question the user has not answered",
              "When the user asked a question, the first line is the answer",
              "Where a test-first process or the plan names tests, follow that instead",
              "Once you return, you are finished",
              'grep -rn "lean:\\|ponytail:"'):
        assert s in text, s


CHECKS = [session_start, subagent, subagent_no_id, guard, bad_settings, crlf, broken_stdin, fixed_rules]


def main():
    for check in CHECKS:
        check()
    print("pass")


if __name__ == "__main__":
    main()
