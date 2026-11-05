#!/usr/bin/env python3
"""A project's core documents in docs/core: inject the index at session start, find stale documents, keep them short and clean. Standard library only.

Manifest, docs/core/core.json:
{"refresh_days": 1, "skip": ["runs"], "docs": [{"file": "goals.md", "brief": "what it holds", "sources": ["~memory", "src/*.py"]}]}
The script adds "reviewed" and "goal_sha" to it. `*` also crosses folders. `~memory` is this project's Claude Code memory folder.
"""
import argparse
import fnmatch
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

CORE = Path("docs/core")
INDEX_LINES, INDEX_CHARS = 40, 2500
DOC_LINES = 80
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".phro-project", "venv", ".venv"}
SHOWN = 8

# The same lists as the style check in phro-search, cut to what a reference document can trip on.
MARKS = ["Generated with", "Co-Authored-By", "Written by Claude", "AI-generated", "as an AI", "Claude가 작성", "AI가 작성", "AI 요약", "언어 모델로서",
         "알아보겠습니다", "살펴보겠습니다", "결론적으로", "요약하자면", "정리하자면", "혁신적", "획기적", "게임 체인저", "게임체인저", "패러다임 전환",
         "in conclusion", "in summary", "to summarize", "worth noting", "important to note", "game-changer", "groundbreaking", "revolutionary", "cutting-edge"]
TELLS = re.compile("[–—―​‌‍⁠﻿\U0001F000-\U0001FAFF☀-➿■-◿⬀-⯿]")
HISTORY = re.compile(r"변경\s*이력|개정\s*이력|수정\s*이력|업데이트\s*내역|change\s*log|revision history|version history", re.I)
DATE = re.compile(r"(?<!\d)20\d\d[-./]\d{1,2}[-./]\d{1,2}(?!\d)")
CHAIN = re.compile(r"\bv\d+\s*(?:->|→)\s*v\d+", re.I)
TICKED = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\]\(([^)\s]+)\)")


def load(root):
    return json.loads((Path(root) / CORE / "core.json").read_text(encoding="utf-8"))


def save(root, manifest):
    path = Path(root) / CORE / "core.json"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(manifest, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    os.replace(temp, path)


def memory_dir(root):
    return Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve())) / "memory"


def source_files(root, pattern, skip):
    """Files behind one source pattern. Walks only below the pattern's literal prefix, since a project can hold 200k files."""
    if pattern == "~memory":
        yield from memory_dir(root).glob("*.md")
        return
    root = Path(root)
    parts = pattern.split("/")
    fixed = []
    for part in parts:
        if "*" in part or "?" in part:
            break
        fixed.append(part)
    start = root.joinpath(*fixed)
    if start.is_file():
        yield start
        return
    wanted = pattern if len(fixed) < len(parts) else pattern.rstrip("/") + "/*"
    for folder, dirs, files in os.walk(start):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and d not in skip and not (Path(folder) / d / "pyvenv.cfg").exists()]
        for name in files:
            path = Path(folder) / name
            if fnmatch.fnmatch(path.relative_to(root).as_posix(), wanted):
                yield path


def stale(root, manifest):
    """Rows of (doc, changed source paths, days since review, never reviewed)."""
    skip = set(manifest.get("skip", []))
    reviewed = manifest.get("reviewed", {})
    now = time.time()
    rows = []
    for doc in manifest["docs"]:
        since = reviewed.get(doc["file"], 0)
        changed = []
        for pattern in doc.get("sources", []):
            for path in source_files(root, pattern, skip):
                try:
                    if path.stat().st_mtime > since:
                        changed.append(path)
                except OSError:
                    continue
        rows.append((doc["file"], changed, (now - since) / 86400 if since else None))
    return rows


def goal_section(text):
    """The first level-2 section of goals.md: the final goal, in the user's own words."""
    found = re.search(r"^## .*?(?=^## |\Z)", text, re.M | re.S)
    return found.group(0).strip() if found else ""


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def path_findings(root, doc, text):
    out = []
    candidates = []
    for line_no, line in enumerate(text.splitlines(), 1):
        candidates += [(line_no, t) for t in TICKED.findall(line)]
        candidates += [(line_no, t) for t in LINK.findall(line)]
    for line_no, token in candidates:
        token = re.sub(r"(:\d+|#.*)$", "", token.strip()).rstrip("/")
        if not token or any(c in token for c in " <>*${}") or "://" in token or token.startswith(("~", "/", "#")) or ":" in token:
            continue
        if "/" not in token and not re.search(r"\.[A-Za-z][A-Za-z0-9]{0,5}$", token):
            continue
        if not ((Path(root) / token).exists() or (doc.parent / token).exists()):
            out.append(("dead-path", f"{doc.name}:{line_no}", token))
    return out


def lint(root, accept_goal=False):
    root = Path(root)
    manifest = load(root)
    core = root / CORE
    out = []
    index = core / "README.md"
    files = [d["file"] for d in manifest["docs"]]
    if not index.exists():
        return [("missing", "README.md", "the index is what sessions receive")]
    listed = index.read_text(encoding="utf-8")
    lines, chars = len(listed.splitlines()), len(listed)
    if lines > INDEX_LINES or chars > INDEX_CHARS:
        out.append(("long", "README.md", f"{lines} lines, {chars} chars, limit {INDEX_LINES} and {INDEX_CHARS}"))
    for name in files:
        if name not in listed:
            out.append(("unlisted", name, "the index does not name it"))
    known = {(core / f).resolve() for f in files} | {index.resolve()}
    for path in sorted(core.rglob("*.md")):
        if path.resolve() not in known:
            out.append(("no-manifest", path.relative_to(core).as_posix(), "not in core.json"))
    for name in ["README.md"] + files:
        path = core / name
        if not path.exists():
            out.append(("missing", name, "listed in core.json"))
            continue
        text = path.read_text(encoding="utf-8")
        if name != "README.md" and len(text.splitlines()) > DOC_LINES:
            out.append(("long", name, f"{len(text.splitlines())} lines, limit {DOC_LINES}"))
        for line_no, line in enumerate(text.splitlines(), 1):
            where = f"{name}:{line_no}"
            quote = line.lstrip().startswith(">")
            lowered = line.lower()
            for mark in MARKS:
                if mark.lower() in lowered:
                    out.append(("watermark", where, mark))
            found = TELLS.search(line)
            if found and not quote:
                out.append(("tell", where, f"U+{ord(found.group(0)):04X}"))
            if HISTORY.search(line) or CHAIN.search(line):
                out.append(("history", where, "state what is true now, the record belongs elsewhere"))
            if DATE.search(line) and not quote:
                out.append(("dated", where, DATE.search(line).group(0)))
        out += path_findings(root, path, text)
    goals = core / "goals.md"
    if goals.exists() and manifest.get("goal_sha") and not accept_goal:
        if sha(goal_section(goals.read_text(encoding="utf-8"))) != manifest["goal_sha"]:
            out.append(("goal-changed", "goals.md", "the final goal moved: only the user may change it, then pass --accept-goal"))
    return out


def stamp(root, names, accept_goal=False):
    found = lint(root, accept_goal)
    if found:
        return found
    manifest = load(root)
    now = time.time()
    reviewed = manifest.setdefault("reviewed", {})
    for doc in manifest["docs"]:
        if not names or doc["file"] in names:
            reviewed[doc["file"]] = now
    goals = Path(root) / CORE / "goals.md"
    if goals.exists() and (accept_goal or not manifest.get("goal_sha")):
        manifest["goal_sha"] = sha(goal_section(goals.read_text(encoding="utf-8")))
    save(root, manifest)
    return []


def session_text(root):
    """What a session receives. Reads two small files and never walks the project, so it stays fast everywhere."""
    core = Path(root) / CORE
    index = core / "README.md"
    if not index.is_file():
        return ""
    parts = ["Project core docs. Read the one for your work area before a large decision. "
             "When the user changes a goal or a design here, update that doc in the same step."]
    if (core / "structure.md").exists():
        parts[0] += " New files go where docs/core/structure.md says."
    parts.append(index.read_text(encoding="utf-8").strip())
    try:
        manifest = load(root)
        reviewed = manifest.get("reviewed", {})
        days = (time.time() - min(reviewed.values())) / 86400 if reviewed else None
        if days is not None and days > manifest.get("refresh_days", 1) + 1:
            parts.append(f"Oldest core doc review was {days:.0f} days ago. The scheduled refresh may be off: tell the user, or run /phro-project:compass refresh at a stopping point.")
    except (OSError, ValueError):
        pass
    return "\n\n".join(parts)


def hook():
    """A hook must never break a session, so every failure ends silently."""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    try:
        text = session_text(os.environ.get("CLAUDE_PROJECT_DIR") or event.get("cwd") or ".")
    except OSError:
        return
    if text:
        print(text)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("hook", help="print the index for a session; silent where docs/core does not exist")
    sub.add_parser("status", help="documents whose sources changed since their last review").add_argument("root")
    sub.add_parser("lint", help="length, history, dates, AI marks, dead paths, a moved final goal").add_argument("root")
    p = sub.add_parser("stamp", help="mark documents reviewed, only when lint is clean")
    p.add_argument("root")
    p.add_argument("files", nargs="*")
    p.add_argument("--accept-goal", action="store_true", help="the user changed the final goal")
    args = parser.parse_args()
    if args.command == "hook":
        return hook()
    if args.command == "status":
        manifest = load(args.root)
        count = 0
        for name, changed, age in stale(args.root, manifest):
            state = "never-reviewed" if age is None else ("stale" if changed else "ok")
            count += state != "ok"
            print(f"{state}\t{name}\t{len(changed)} changed\t{'-' if age is None else f'{age:.1f}d'}")
            for path in sorted(changed, key=lambda p: -p.stat().st_mtime)[:SHOWN]:
                print(f"  {path.as_posix()}")
        print(f"STALE\t{count}")
        return
    found = lint(args.root) if args.command == "lint" else stamp(args.root, args.files, args.accept_goal)
    for row in found:
        print("\t".join(row))
    print(f"FINDINGS\t{len(found)}" if found else "OK")
    sys.exit(1 if found else 0)


if __name__ == "__main__":
    main()
