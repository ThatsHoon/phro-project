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
              "Write no comments",
              "The brief's bar is the whole bar",
              "propose in the same reply the strongest enforcement that fits"):
        assert s in text, s


def prompt(text, sid, cwd):
    return {"hook_event_name": "UserPromptSubmit", "prompt": text, "session_id": sid, "cwd": str(cwd)}


def toggle():
    import lean
    config, cwd = fresh()
    assert run(prompt("phro-lean off", "s1", cwd), config, cwd)[1].strip() == lean.OFF_TEXT
    assert run(start("s1", cwd, "compact"), config, cwd)[1] == ""
    assert run(sub("s1", cwd), config, cwd)[1] == ""
    assert "## Build" in run(start("s2", cwd), config, cwd)[1], "another session is untouched"
    assert run(prompt("/phro-lean on", "s1", cwd), config, cwd)[1].strip() == lean.main_text().strip()
    assert "## Build" in run(start("s1", cwd, "compact"), config, cwd)[1]
    run(prompt("/phro-project:lean OFF ", "s1", cwd), config, cwd)
    assert run(start("s1", cwd, "resume"), config, cwd)[1] == ""


def copy_toggle():
    import lean
    config, cwd = fresh()
    assert run(prompt("/other-plugin:lean off", "s1", cwd), config, cwd)[1] == ""
    assert run(prompt("/phro-project_e-call:lean off", "s1", cwd), config, cwd)[1].strip() == lean.OFF_TEXT
    assert run(sub("s1", cwd), config, cwd)[1] == ""


def mention_only():
    config, cwd = fresh()
    for text in ("phro-lean off 하면 어떻게 돼?", "please run\nphro-lean off", "what is phro-lean"):
        assert run(prompt(text, "s1", cwd), config, cwd)[1] == "", text
    assert not (config / "phro-lean").exists()
    assert "## Build" in run(start("s1", cwd), config, cwd)[1]


def bad_id():
    config, cwd = fresh()
    run(prompt("phro-lean off", "../x", cwd), config, cwd)
    assert not [p for p in config.rglob("*") if p.is_file()], list(config.rglob("*"))
    assert "## Build" in run(start("../x", cwd), config, cwd)[1]


def guard_toggle():
    import lean
    config, cwd = fresh()
    write(config / "settings.json", json.dumps({"enabledPlugins": {"ponytail@ponytail": True}}))
    assert run(prompt("phro-lean off", "s1", cwd), config, cwd)[1].strip() == lean.OFF_TEXT + lean.GUARD_NOTE


def prune():
    config, cwd = fresh()
    folder = config / "phro-lean/off"
    folder.mkdir(parents=True)
    for name in ("old", "new", "notes.txt"):
        (folder / name).write_text("", encoding="utf-8")
    week_ago = (folder / "old").stat().st_mtime - 8 * 86400
    os.utime(folder / "old", (week_ago, week_ago))
    os.utime(folder / "notes.txt", (week_ago, week_ago))
    run(start("s9", cwd, "compact"), config, cwd)
    assert (folder / "old").exists(), "only startup prunes"
    run(start("s9", cwd, "startup"), config, cwd)
    assert not (folder / "old").exists() and (folder / "new").exists() and (folder / "notes.txt").exists()


def hooks_json():
    cmd = 'python -I -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/lean.py" hook'
    hooks = json.loads((ROOT / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    starts = hooks["SessionStart"]
    assert any("compass.py" in h["command"] and e["matcher"] == "startup|resume|clear|compact" for e in starts for h in e["hooks"])
    assert any(e.get("matcher") == "startup|resume|clear|compact" and e["hooks"] == [{"type": "command", "command": cmd, "timeout": 5}]
               for e in starts), starts
    lean = {"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}
    assert hooks["SubagentStart"] == [lean], hooks.get("SubagentStart")
    assert hooks["UserPromptSubmit"][0] == lean, "lean first; the queue hook may follow"
    queue = 'python -I -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/queue.py" hook'
    daily = 'python -I -X utf8 "${CLAUDE_PLUGIN_ROOT}/scripts/daily.py" hook'
    assert hooks["UserPromptSubmit"][1:] == [{"hooks": [{"type": "command", "command": c, "timeout": 5}]} for c in (queue, daily)], hooks["UserPromptSubmit"]
    assert any(h["command"] == queue for e in starts for h in e["hooks"]), starts


def has_all(rel, strings):
    text = (ROOT / rel).read_text(encoding="utf-8")
    for s in strings:
        assert s in text, (rel, s)


def grill_rules():
    has_all("skills/grill/SKILL.md", ("one line of rationale", "grill-<topic>.md", "at most 5 questions",
                                      "When at most 2 decisions remain and both are cheap to reverse",
                                      "restate that decision's cost in one line", 'Word each question so "yes" accepts',
                                      "Finding facts is your job"))


def lean_review_rules():
    has_all("skills/lean-review/SKILL.md", ("delete:", "stdlib:", "native:", "yagni:", "shrink:", "net: -<N> lines possible.",
                                            "Lean already. Ship.", "Correctness bugs, security holes and performance are out of scope",
                                            "Does not apply the fixes"))


def review_fixes():
    """Final-review fixes: each phrase pins one finding."""
    has_all("skills/lean/SKILL.md", ("Ask a question at most once",
                                     "never drop a finding to fit",
                                     "when you can write files and the brief allows it",
                                     "skills in use (TDD, brainstorming, plans, grill and the like)",
                                     "In a subagent, the brief counts as the user's words"))
    has_all("skills/lean-review/SKILL.md", ("git diff HEAD", "untracked"))


def comment_rules():
    import lean
    text = SKILL.read_text(encoding="utf-8")
    for injected in (lean.main_text(), lean.sections(text, lean.SUB)):
        assert "Write no comments" in injected and "| when:" not in injected and "lean: <" not in injected, injected[:200]
    review = (ROOT / "skills/lean-review/SKILL.md").read_text(encoding="utf-8")
    assert "`comment:`" in review and "`debt:`" not in review and "grep -rn" not in review
    compass_skill = (ROOT / "skills/compass/SKILL.md").read_text(encoding="utf-8")
    assert "debt" not in compass_skill and "(hook: guard.py)" in compass_skill
    assert "5b. A repeated mistake" in (ROOT / "skills/design/SKILL.md").read_text(encoding="utf-8")


def resume_once():
    config, cwd = fresh()
    assert "## Build" in run(start("s1", cwd), config, cwd)[1]
    assert run(start("s1", cwd, "resume"), config, cwd)[1] == "", "a resume that holds the same text gets nothing"
    assert "## Build" in run(start("s2", cwd, "resume"), config, cwd)[1], "another session gets the rules"
    assert "## Build" in run(start("s1", cwd, "compact"), config, cwd)[1], "a compaction drops the rules, so they come back"
    assert "## Build" in run(start("s3", cwd, "resume"), config, cwd)[1]
    seen = config / "phro-project/seen/s3.lean"
    seen.write_text("stale", encoding="ascii")
    assert "## Build" in run(start("s3", cwd, "resume"), config, cwd)[1], "changed rules are sent again"
    old = seen.stat().st_mtime - 15 * 86400
    os.utime(seen, (old, old))
    run(start("s4", cwd), config, cwd)
    assert not seen.exists(), "startup prunes old records"


def read_only_subagent():
    config, cwd = fresh()
    for kind in ("Explore", "Plan", "claude-code-guide", "web-fetch"):
        event = dict(sub("s1", cwd), agent_type=kind)
        ctx = json.loads(run(event, config, cwd)[1])["hookSpecificOutput"]["additionalContext"]
        assert "## Report" in ctx and "## Build" not in ctx, (kind, ctx[:200])
    for kind in ("general-purpose", "my-plugin:writer"):
        event = dict(sub("s1", cwd), agent_type=kind)
        assert "## Build" in json.loads(run(event, config, cwd)[1])["hookSpecificOutput"]["additionalContext"], kind


def stats():
    import lean
    base = Path(tempfile.mkdtemp())

    def session(name, header, replies):
        lines = []
        if header:
            lines.append(json.dumps({"type": "attachment", "attachment": {"type": "hook_additional_context", "content": [lean.HEADER + " rules"]}}))
        for text in replies:
            lines.append(json.dumps({"type": "user", "message": {"content": "q"}}))
            lines.append(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}, {"type": "tool_use", "name": "Read"}]}}))
        (base / f"{name}.jsonl").write_text("\n".join(lines) + "\n{broken\n", encoding="utf-8")

    big = "Done.\n\n" + "\n".join(f"- item {i}" for i in range(6))
    numbered = "\n".join(f"{i}. step" for i in range(1, 7))
    session("a", True, ["Done.", big])
    session("b", True, ["One?\n\nTwo? Three?"])
    session("c", False, [numbered, "A? B?", "plain"])
    session("d", False, [])
    old = base / "old.jsonl"
    old.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}) + "\n", encoding="utf-8")
    t = old.stat().st_mtime - 30 * 86400
    os.utime(old, (t, t))
    rows = lean.stats(base, 7)
    assert rows == ["arm\tsessions\treplies\tbig_list\tmulti_question",
                    "lean\t2\t3\t33.3%\t33.3%",
                    "none\t1\t3\t33.3%\t33.3%"], rows
    assert lean.reply_flags("- a\n- b\n\n- c\n- d\n- e\n- f") == (False, False), "a blank line ends a group"
    res = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "stats", str(base), "--days", "60"],
                         capture_output=True, text=True, encoding="utf-8")
    assert res.returncode == 0 and res.stdout.splitlines()[2].startswith("none\t2\t4\t"), res.stdout + res.stderr


CHECKS = [session_start, subagent, subagent_no_id, guard, bad_settings, crlf, broken_stdin, fixed_rules,
          toggle, copy_toggle, mention_only, bad_id, guard_toggle, prune, hooks_json, grill_rules, lean_review_rules, review_fixes,
          comment_rules, resume_once, read_only_subagent, stats]


def main():
    for check in CHECKS:
        check()
    print("pass")


if __name__ == "__main__":
    main()
