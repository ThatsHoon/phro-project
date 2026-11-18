"""Read a plugin's current structure and measure whether its skills get called. Standard library only.

<plugin> is a plugin folder, or a project root, which means the skills in its .claude.

Structure, rebuilt from the plugin's files on every call (the files are the source, the database is a throwaway copy):
  project.py summary <plugin> [--usage DIR]     nodes, and per skill its token cost, tests and use; with --usage, reads of each reference
  project.py check   <plugin> [--usage DIR]     deterministic findings, one per line
  project.py impact  <plugin> <node>            what depends on it, and what only it uses
  project.py sql     <plugin> "<select>"        read-only query: node(id, kind, path, description, desc_tokens, body_tokens), edge(src, dst, how)

Routing, on a scratch copy so the plugin itself is never edited:
  project.py mine    <transcripts_dir> <cases.json> [--alias a=b ...] [--only a,b]   prompts labelled by the command the user typed
  project.py catalog <out.json> <skills_dir> [<skills_dir> ...] [--only a,b]         competing descriptions from installed skills
  project.py build   <plugin> <cases.json> <out_dir> [--history f.jsonl] [--stub] [--also out.json] [--max-turns N]
  project.py tally   <result.json> [<result.json> ...] [--cases cases.json]         hits, misses, which skill took each miss; held-out cases apart

Output quality, from a `claude plugin eval --ablation with-without` run (graders named b-* are boundaries):
  project.py quality <result.json> [--accepted accepted.json] [--save accepted.json]  per-grader pass rate in each arm, breaks, gained and broken, and a stale line when the model or the skill changed since
"""
import argparse
import collections
import hashlib
import json
import re
import shutil
import sqlite3
import sys
from pathlib import Path

SKIP = {".git", "__pycache__", "node_modules", "results", ".venv"}
TEXT = {".md", ".py", ".json", ".yaml", ".yml", ".sh", ".js", ".ts", ".tsx", ".cjs", ".mjs", ".toml", ".txt", ".ps1", ".jsonl"}
LISTING_LIMIT = 1536  # description + when_to_use, cut off in the skill listing beyond this
LONG_BODY_TOKENS = 5000  # a compaction re-attaches a skill cut to its first 5,000 tokens
OVERLAP_MIN = 0.25
SHARED_LINES_MIN = 3
PATH_REF = re.compile(r"(?<![\w./-])((?:references|scripts|hooks|agents|skills|commands|assets|templates|fixtures)/[\w./-]*\w\.\w+)")
MD_LINK = re.compile(r"\]\(([^)#:\s<>]+\.\w+)\)")
IMPORT = re.compile(r"^\s*(?:from|import)\s+(\w+)", re.M)
STOP = set("the and for with use when this that from not you your any are can into only about all its it's run runs used using skill skills user users asks wants whenever even".split())
PROMPT_MIN, PROMPT_MAX = 10, 200
TOOLS = "[Read, Glob, Grep, WebSearch, Skill]"
ROUTE_CASES = "tests/route_cases.json"
PROJECT_PARTS = ("skills", "agents", "commands", "tests")
STEP_TOOLS = "[Read, Glob, Grep, WebSearch, Write, Edit, Skill]"
STEP_TURNS = 10
FETCHERS = {"npx", "bunx", "uvx", "pipx"}
PINNED = re.compile(r"(?:@|==)v?\d[\w.+-]*$")
LEAK = re.compile(r"\b(?:evals?|evaluation|judge[sd]?|grader?s?|rubrics?|benchmarks?|candidates?|test case|this is a test)\b|평가|채점|시험 삼아|테스트용|벤치마크", re.I)
# From code.claude.com/docs/en/skills and /hooks. A key or event Claude Code does not know is ignored without an error.
SKILL_KEYS = set("name description when_to_use argument-hint arguments disable-model-invocation user-invocable allowed-tools "
                 "disallowed-tools model effort context agent background hooks paths shell metadata license compatibility version".split())
HOOK_EVENTS = set("SessionStart Setup UserPromptSubmit UserPromptExpansion PreToolUse PermissionRequest PermissionDenied PostToolUse "
                  "PostToolUseFailure PostToolBatch Notification MessageDisplay SubagentStart SubagentStop TaskCreated TaskCompleted "
                  "Stop StopFailure TeammateIdle InstructionsLoaded ConfigChange CwdChanged DirectoryAdded FileChanged WorktreeCreate "
                  "WorktreeRemove PreCompact PostCompact PreModelSwitch PostModelSwitch Elicitation ElicitationResult SessionEnd".split())
SKILL_CALL = '"skill"\\s*:\\s*"(?:[\\w-]+:)?{}"'


def tokens(text):
    return len(text.encode("utf-8")) // 4


def plugin_root(path):
    path = Path(path).resolve()
    if not (path / ".claude-plugin").exists() and not (path / "skills").is_dir() and (path / ".claude/skills").is_dir():
        return path / ".claude"
    return path


def parts_of(root):
    return [root / part for part in PROJECT_PARTS if (root / part).exists()] if root.name == ".claude" else [root]


def frontmatter(text):
    m = re.match(r"---\r?\n(.*?)\r?\n---\r?\n?", text, re.S)
    if not m:
        return {}, text
    fields, key = {}, None
    for line in m.group(1).splitlines():
        kv = re.match(r"([\w-]+):\s*(.*)$", line)
        if kv:
            key = kv.group(1)
            fields[key] = kv.group(2).strip()
        elif key:
            fields[key] += " " + line.strip()
    return {k: re.sub(r"^(>-?|\|-?)\s*", "", v).strip().strip("'\"") for k, v in fields.items()}, text[m.end():]


def user_only(fields):
    return fields.get("disable-model-invocation") == "true"


def classify(rel):
    parts = rel.split("/")
    name = parts[-1]
    if parts[0] == "skills" and name.lower() == "skill.md":
        return "skill", f"skill:{parts[-2]}"
    if parts[0] == "agents" and name.endswith(".md"):
        return "agent", f"agent:{Path(name).stem}"
    if parts[0] == "commands" and name.endswith(".md"):
        return "command", f"command:{Path(name).stem}"
    if parts[0] == "evals" and len(parts) > 2:
        return "eval", f"eval:{parts[1]}"
    if rel == ROUTE_CASES:
        return "eval", "eval:route-cases"
    if rel in ("hooks/hooks.json", ".mcp.json", ".claude-plugin/plugin.json"):
        return "config", rel
    suffix = Path(name).suffix
    if suffix in {".py", ".js", ".ts", ".tsx", ".cjs", ".mjs", ".sh", ".ps1"}:
        return "script", rel
    return ("doc" if suffix in TEXT else "asset"), rel


def scan(root):
    root = Path(root).resolve()
    manifest_path = root / ".claude-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    plugin = manifest.get("name", root.name)
    db = sqlite3.connect(":memory:")
    db.executescript("""
        create table node(id text primary key, kind text, path text, description text, desc_tokens int, body_tokens int);
        create table edge(src text, dst text, how text, primary key(src, dst, how));""")
    texts, sources, kinds = collections.defaultdict(str), collections.defaultdict(list), {}
    for path in sorted(found for part in parts_of(root) for found in part.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or SKIP & set(rel.split("/")):
            continue
        kind, node = classify(rel)
        kinds[node] = kind
        sources[node].append(rel)
        if path.suffix in TEXT:
            texts[node] += path.read_text(encoding="utf-8", errors="replace") + "\n"
    servers = manifest.get("mcpServers") if isinstance(manifest.get("mcpServers"), dict) else {}
    if (root / ".mcp.json").exists():
        loaded = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
        servers = {**servers, **loaded.get("mcpServers", loaded)}
    for name, entry in servers.items():
        node = f"mcp:{name}"
        kinds[node], texts[node] = "mcp", json.dumps(entry)
        sources[node] = []
    for node, kind in kinds.items():
        fields, body = frontmatter(texts[node]) if kind in ("skill", "agent", "command") else ({}, texts[node])
        listing = " ".join(filter(None, [fields.get("description"), fields.get("when_to_use")]))
        where = sources[node][0] if sources[node] else ""
        cost = 0 if user_only(fields) else tokens(listing)
        db.execute("insert into node values(?,?,?,?,?,?)", (node, kind, where, listing, cost, tokens(body)))
    add_edges(db, plugin, kinds, texts, sources)
    return db, plugin, texts, sources


def add_edges(db, plugin, kinds, texts, sources):
    names = collections.Counter(Path(s).name for files in sources.values() for s in files)
    targets = [(node, rel) for node, files in sources.items() for rel in files if kinds[node] not in ("eval", "config")]
    named = {n.split(":", 1)[1]: n for n in kinds if kinds[n] in ("skill", "agent", "command")}
    modules = {Path(rel).stem: node for node, rel in targets if rel.endswith(".py")}
    for src, text in texts.items():
        here = [Path(s).parent.as_posix() for s in sources[src]]
        for node, rel in targets:
            if node == src:
                continue
            base = Path(rel).name
            forms = {rel} | {rel[len(h) + 1:] for h in here if h != "." and rel.startswith(h + "/")}
            inside = any(h != "." and rel.startswith(h + "/") for h in here)
            if (names[base] == 1 or inside) and "." in base and len(base) > 5:
                forms.add(base)
            if any(f in text and re.search(r"(?<![\w-])" + re.escape(f) + r"(?![\w-])", text) for f in forms):
                db.execute("insert or ignore into edge values(?,?,'path')", (src, node))
        if any(s.endswith(".py") for s in sources[src]):
            for module in IMPORT.findall(text):
                if module in modules and modules[module] != src:
                    db.execute("insert or ignore into edge values(?,?,'import')", (src, modules[module]))
        for name, node in named.items():
            if node == src:
                continue
            called = re.search(r"(?<![\w-])" + re.escape(f"{plugin}:{name}") + r"(?![\w-])", text)
            if src == "eval:route-cases":
                graded = re.search(r'"skill"\s*:\s*"' + re.escape(name) + '"', text)
            else:
                graded = kinds[src] == "eval" and re.search(r"(?m)^.*(?:input_match|skill).*(?<![\w-])" + re.escape(name) + r"(?![\w-]).*$", text)
            if called or graded:
                db.execute("insert or ignore into edge values(?,?,'name')", (src, node))


def usage(directory, plugin):
    typed, model, reads, seen = collections.Counter(), collections.Counter(), collections.Counter(), set()
    paths = list(Path(directory).rglob("*.jsonl"))
    if not paths:
        return None
    for path in paths:
        for line in path.open(encoding="utf-8", errors="replace"):
            if '"Skill"' not in line and "<command-name>" not in line and not ('"Read"' in line and "skills" in line):
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            uid = e.get("uuid")
            if uid in seen:
                continue
            seen.add(uid)
            content = (e.get("message") or {}).get("content")
            if e.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Read":
                        reads[str((b.get("input") or {}).get("file_path", "")).replace("\\", "/")] += 1
            if e.get("isSidechain"):
                continue
            if e.get("type") == "user":
                text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
                m = re.search(r"<command-name>/?([^<\s]+)</command-name>", text[:3000])
                if m:
                    typed[m.group(1)] += 1
            elif e.get("type") == "assistant" and isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Skill":
                        model[str((b.get("input") or {}).get("skill"))] += 1

    def count(counter, name):
        return counter[name] + counter[f"{plugin}:{name}"]

    def used(name):
        return count(typed, name), count(model, name)
    used.reads = lambda rel: sum(n for path, n in reads.items() if path.endswith("/" + rel))
    return used


def references(sources, kinds):
    return sorted(rel for node, files in sources.items() if kinds.get(node) == "doc" for rel in files
                  if rel.startswith("skills/") and rel.endswith(".md") and Path(rel).name not in ("SKILL.md", "CONTRACT.md"))


def fetched_package(entry):
    command = re.sub(r"\.(?:cmd|exe)$", "", Path(str(entry.get("command", ""))).name.lower())
    args = [str(x) for x in entry.get("args") or []]
    if command not in FETCHERS:
        return None
    for flag in ("--from", "--package", "-p", "--spec"):
        if flag in args[:-1]:
            return args[args.index(flag) + 1]
    return next((x for x in args if not x.startswith("-") and x != "run"), None)


FIX = {
    "broken-ref": "write the path it has now, or remove the mention",
    "duplicate-file": "keep one and point the others at it, unless the copy is deliberate",
    "shared-lines": "move the shared lines into one file both read, or cut them from one",
    "orphan": "delete it, or name it where it is used",
    "mcp-unpinned": "pin an exact version",
    "hook-config": "use a known event and give every command a timeout",
    "leaky-case": "rewrite the prompt as a user would type it, without measurement words",
    "unknown-frontmatter": "remove the key, or spell it as Claude Code reads it",
    "no-eval": "add cases to tests/route_cases.json as the design skill describes",
    "no-description": "write one: what it does in a clause, then the words users type",
    "long-description": "cut it below the limit",
    "long-body": "move detail into a references file the body points to",
    "echo-description": "delete the repeated sentence from the body",
    "overlap-candidate": "read both descriptions; fix the overlap, or merge them if one real request fits both",
    "never-called": "check that the transcripts span the skill's life, then sharpen its description or remove it",
    "never-read": "put what it holds where it is needed, or delete it",
    "unmeasured": "pass --usage with this project's transcripts folder",
}


def echoed(description, body):
    flat = " ".join(body.split())
    return [x for x in re.split(r"(?<=[.!?])\s+", " ".join(description.split())) if len(x) >= 40 and x in flat]


def case_findings(root):
    out = []
    try:
        cases = json.loads((root / ROUTE_CASES).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cases = []
    for case in cases if isinstance(cases, list) else []:
        found = LEAK.search(str(case.get("prompt", ""))) if isinstance(case, dict) else None
        if found:
            out.append(("leaky-case", f"{ROUTE_CASES}#{case.get('id', '?')}", found.group(0)))
    for path in sorted(root.glob("evals/quality/*/*/prompt.md")):
        found = LEAK.search(path.read_text(encoding="utf-8", errors="replace"))
        if found:
            out.append(("leaky-case", path.relative_to(root).as_posix(), found.group(0)))
    return out


def hook_findings(root):
    path = root / "hooks/hooks.json"
    if not path.exists():
        return []
    try:
        events = json.loads(path.read_text(encoding="utf-8")).get("hooks") or {}
    except (ValueError, AttributeError):
        return [("hook-config", "hooks/hooks.json", "not a JSON object with a hooks map")]
    out = []
    for event, groups in events.items():
        if event not in HOOK_EVENTS:
            out.append(("hook-config", "hooks/hooks.json", f"{event} is not a hook event"))
            continue
        for hook in (h for g in groups for h in g.get("hooks") or []):
            if hook.get("type") == "command" and "timeout" not in hook and not hook.get("async"):
                out.append(("hook-config", "hooks/hooks.json", f"{event} command has no timeout, so it can hold the session for the default"))
    return out


def findings(root, usage_dir=None):
    root = Path(root).resolve()
    db, plugin, texts, sources = scan(root)
    out = []
    nodes = db.execute("select id, kind, description, desc_tokens from node").fetchall()
    kinds = {n[0]: n[1] for n in nodes}
    every = [rel for files in sources.values() for rel in files]
    instructions = {rel for node, files in sources.items() if kinds[node] in ("skill", "agent", "command", "config") for rel in files}
    for rel in every:
        scripted = rel.startswith(("skills/", "scripts/", "hooks/")) and Path(rel).suffix in {".py", ".js", ".ts", ".tsx", ".cjs", ".mjs", ".sh", ".ps1"}
        if rel not in instructions and not scripted:
            continue
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        named = set(PATH_REF.findall(text))
        links = set(MD_LINK.findall(text)) if rel.endswith(".md") else set()
        for ref in sorted(named | links):
            found = (root / ref).exists() or ((root / rel).parent / ref).exists() or any(f.endswith("/" + ref) for f in every)
            head = ref.removeprefix("./").split("/")[0]
            example = ref not in named and "/" in ref and not (root / head).exists() and not ((root / rel).parent / head).exists()
            if not found and not example:
                out.append(("broken-ref", rel, ref))
    by_hash = collections.defaultdict(list)
    for node, files in sources.items():
        if kinds[node] == "eval":
            continue
        for rel in files:
            if Path(rel).suffix in TEXT and (root / rel).stat().st_size >= 200:
                by_hash[hashlib.sha256(" ".join((root / rel).read_text(encoding="utf-8", errors="replace").split()).encode()).hexdigest()].append(rel)
    out += [("duplicate-file", group[0], ", ".join(group[1:])) for group in by_hash.values() if len(group) > 1]
    long_lines = {n: {" ".join(line.split()) for line in frontmatter(texts[n])[1].splitlines() if len(line.strip()) >= 40}
                  for n, k in kinds.items() if k in ("skill", "agent", "command")}
    pairs = sorted(long_lines)
    for i, a in enumerate(pairs):
        for b in pairs[i + 1:]:
            shared = len(long_lines[a] & long_lines[b])
            if shared >= SHARED_LINES_MIN:
                out.append(("shared-lines", a, f"{b}: {shared} lines"))
    reached = {r[0] for r in db.execute("select distinct dst from edge")}
    for node, kind in kinds.items():
        path = sources[node][0] if sources[node] else ""
        runs = path.startswith(("skills/", "scripts/", "hooks/")) and (kind == "script" or (kind == "doc" and path.endswith(".md")))
        if runs and node not in reached and Path(path).name not in ("__init__.py", "README.md", "CONTRACT.md"):
            out.append(("orphan", node, "no file refers to it"))
    for node, kind in kinds.items():
        package = fetched_package(json.loads(texts[node])) if kind == "mcp" else None
        if package and not PINNED.search(package):
            out.append(("mcp-unpinned", node, package))
    out += hook_findings(root)
    out += case_findings(root)
    tested = {r[0] for r in db.execute("select e.dst from edge e join node n on n.id = e.src where n.kind = 'eval'")}
    words = {}
    for node, kind, description, _ in nodes:
        if kind != "skill":
            continue
        fields = frontmatter(texts[node])[0]
        out += [("unknown-frontmatter", node, key) for key in sorted(fields) if key not in SKILL_KEYS]
        if node not in tested and not user_only(fields):
            out.append(("no-eval", node, "no eval case grades it"))
        if not description:
            out.append(("no-description", node, "the model cannot pick it from the listing"))
        elif len(description) > LISTING_LIMIT:
            out.append(("long-description", node, f"{len(description)} chars, cut at {LISTING_LIMIT}"))
        if tokens(frontmatter(texts[node])[1]) > LONG_BODY_TOKENS:
            out.append(("long-body", node, f"over about {LONG_BODY_TOKENS} tokens, and a compaction cuts the rest"))
        out += [("echo-description", node, x[:60]) for x in echoed(description or "", frontmatter(texts[node])[1])]
        if not user_only(fields):
            words[node] = {w for w in re.findall(r"\w{3,}", description.lower()) if w not in STOP}
    listed = sorted(words)
    for i, a in enumerate(listed):
        for b in listed[i + 1:]:
            union = words[a] | words[b]
            score = len(words[a] & words[b]) / len(union) if union else 0
            if score >= OVERLAP_MIN:
                out.append(("overlap-candidate", a, f"{b}: {score:.2f}"))
    if usage_dir:
        used = usage(usage_dir, plugin)
        if used:
            out += [("never-called", n[0], "0 typed, 0 model calls in these transcripts") for n in nodes if n[1] == "skill" and used(n[0].split(":", 1)[1]) == (0, 0)]
            out += [("never-read", rel, "0 Reads in these transcripts") for rel in references(sources, kinds) if not used.reads(rel)]
        else:
            out.append(("unmeasured", str(usage_dir), "no transcripts here, so use is unknown"))
    return out


def resolve(db, name):
    hits = [r[0] for r in db.execute("select id from node") if r[0] == name or r[0].endswith(":" + name) or r[0].endswith("/" + name)]
    if len(hits) != 1:
        sys.exit(f"node {name!r}: {len(hits)} matches {hits[:5]}")
    return hits[0]


def cmd_summary(a):
    db, plugin, _, sources = scan(a.plugin)
    print(f"plugin\t{plugin}")
    for kind, n in db.execute("select kind, count(*) from node group by kind order by 2 desc"):
        print(f"{kind}\t{n}")
    used = usage(a.usage, plugin) if a.usage else None
    print("skill\tdesc_tok\tbody_tok\tuses\tevals" + ("\ttyped\tmodel" if used else ""))
    query = """select n.id, n.desc_tokens, n.body_tokens,
        (select count(*) from edge e where e.src = n.id),
        (select count(*) from edge e join node m on m.id = e.src where e.dst = n.id and m.kind = 'eval')
        from node n where n.kind = 'skill' order by n.id"""
    for row in db.execute(query):
        extra = "\t%d\t%d" % used(row[0].split(":", 1)[1]) if used else ""
        print("\t".join(map(str, row)) + extra)
    if used:
        kinds = dict(db.execute("select id, kind from node"))
        print("reference\treads")
        for rel in references(sources, kinds):
            print(f"{rel}\t{used.reads(rel)}")


def cmd_check(a):
    out = findings(a.plugin, a.usage)
    for row in out:
        print("\t".join(row + (FIX[row[0]],)))
    print(f"FINDINGS\t{len(out)}")


def cmd_impact(a):
    db = scan(a.plugin)[0]
    node = resolve(db, a.node)
    up = [r[0] for r in db.execute("""with recursive up(id) as (select ? union select e.src from edge e join up on e.dst = up.id)
        select id from up where id != ? order by id""", (node, node))]
    only = [r[0] for r in db.execute("""select e.dst from edge e where e.src = ?
        and not exists (select 1 from edge o where o.dst = e.dst and o.src != ?) order by 1""", (node, node))]
    print(f"node\t{node}")
    print("breaks if removed\t" + (", ".join(up) or "nothing"))
    print("only it uses\t" + (", ".join(only) or "nothing"))


def cmd_sql(a):
    db = scan(a.plugin)[0]
    db.execute("pragma query_only = on")
    for row in db.execute(a.query):
        print("\t".join(map(str, row)))


def cmd_mine(a):
    alias = dict(x.split("=", 1) for x in a.alias)
    only = set(a.only.split(",")) if a.only else None
    seen, cases = set(), []
    for path in sorted(Path(a.transcripts).glob("*.jsonl")):
        for line in path.open(encoding="utf-8", errors="replace"):
            if "<command-args>" not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            content = (e.get("message") or {}).get("content")
            if e.get("type") != "user" or e.get("isSidechain"):
                continue
            text = content if isinstance(content, str) else " ".join(b.get("text", "") for b in content or [] if isinstance(b, dict))
            name = re.search(r"<command-name>/?([^<\s]+)</command-name>", text[:3000])
            args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
            if not name or not args:
                continue
            prompt, skill = args.group(1).strip(), name.group(1).split(":")[-1]
            skill = alias.get(skill, skill)
            pasted = "<" in prompt or "\n\n" in prompt or not PROMPT_MIN <= len(prompt) <= PROMPT_MAX
            if prompt in seen or pasted or (only and skill not in only):
                continue
            seen.add(prompt)
            cases.append({"skill": skill, "prompt": prompt})
    for i, case in enumerate(cases, 1):
        case["id"] = f"m{i:03d}"
    Path(a.out).write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"cases\t{len(cases)}")
    for skill, n in collections.Counter(c["skill"] for c in cases).most_common():
        print(f"{skill}\t{n}")


def cmd_catalog(a):
    only = set(a.only.split(",")) if a.only else None
    out = {}
    for directory in a.dirs:
        for path in sorted(Path(directory).glob("**/SKILL.md")):
            fields, _ = frontmatter(path.read_text(encoding="utf-8", errors="replace"))
            name, text = fields.get("name", path.parent.name), fields.get("description", "")
            if re.fullmatch(r"[\w-]+", name) and len(text) >= 20 and (not only or name in only):
                out.setdefault(name, text[:LISTING_LIMIT])
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"catalog\t{len(out)}")


def grader(name_pattern=None, none=False):
    match = f"input_match: '{SKILL_CALL.format(name_pattern)}'\n" if name_pattern else ""
    limit = "min: 0\nmax: 0\n" if none else ""
    return f"---\ntype: tool_used\ntool: Skill\n{match}{limit}---\n\nGraded from the transcript.\n"


def tool_grader(tool):
    return f"---\ntype: tool_used\ntool: {tool}\n---\n\nGraded from the transcript.\n"


def order_grader(step, name, tool=None):
    after = f"{{ tool: {tool} }}" if tool else f"{{ tool: Skill, input_match: '{SKILL_CALL.format(name)}' }}"
    return f"---\ntype: tool_order\nbefore: {step}\nafter: {after}\n---\n\nGraded from the transcript.\n"


def cmd_build(a):
    src, out = Path(a.plugin).resolve(), Path(a.out).resolve()
    if out == src or src in out.parents:
        sys.exit("out_dir must be outside the plugin")
    if out.exists() and any(out.iterdir()):
        sys.exit(f"{out} exists and may hold an earlier run's results; give a new folder")
    ignore = shutil.ignore_patterns(*SKIP, "evals", "tests")
    if src.name == ".claude":
        for part in parts_of(src):
            if part.name != "tests":
                shutil.copytree(part, out / part.name, ignore=ignore)
    else:
        shutil.copytree(src, out, ignore=ignore)
    skills = {}
    for path in out.glob("skills/*/SKILL.md"):
        skills[frontmatter(path.read_text(encoding="utf-8"))[0].get("name", path.parent.name)] = path
    if a.also:
        for name, description in json.loads(Path(a.also).read_text(encoding="utf-8")).items():
            if name not in skills:
                skills[name] = out / "skills" / name / "SKILL.md"
                skills[name].parent.mkdir(parents=True)
                skills[name].write_text(f"---\nname: {name}\ndescription: {json.dumps(description, ensure_ascii=False)}\n---\n", encoding="utf-8")
    if a.stub:
        for path in skills.values():
            head = re.match(r"---\r?\n.*?\r?\n---\r?\n?", path.read_text(encoding="utf-8"), re.S).group(0)
            path.write_text(head + "\nStop here and reply with the single word: loaded.\n", encoding="utf-8")
    cases = json.loads(Path(a.cases).read_text(encoding="utf-8"))
    for case in cases:
        d = out / "evals" / case["id"]
        (d / "graders").mkdir(parents=True)
        step = case.get("after")
        rule = f"append_system_prompt: {json.dumps(case['rule'], ensure_ascii=False)}\n" if case.get("rule") else ""
        tool = case.get("tool")
        tools = STEP_TOOLS if step else TOOLS
        tools = tools[:-1] + f", {tool}]" if tool else tools
        head = f"max_turns: {max(a.max_turns, STEP_TURNS)}\ntimeout_seconds: 240\nallowed_tools: {tools}\n" if step else f"max_turns: {a.max_turns}\ntimeout_seconds: 180\nallowed_tools: {tools}\n"
        (d / "prompt.md").write_text(f"---\n{head}{rule}---\n\n{case['prompt']}\n", encoding="utf-8")
        target = case.get("skill")
        (d / "graders/target.md").write_text(tool_grader(tool) if tool else grader(target) if target else grader(none=True), encoding="utf-8")
        if step:
            (d / "graders/order.md").write_text(order_grader(step, target, tool), encoding="utf-8")
        for name in skills:
            (d / f"graders/to-{name}.md").write_text(grader(name), encoding="utf-8")
        if a.history:
            shutil.copy(a.history, d / "history.jsonl")
            (d / "case.yaml").write_text(f"schema_version: \"1.1\"\nname: {case['id']}\ncontext:\n  history_file: history.jsonl\n", encoding="utf-8")
    print(f"built\t{out}\tskills\t{len(skills)}\tcases\t{len(cases)}")
    grant = " --allow-tools Write Edit" if any(case.get("after") for case in cases) else ""
    print(f'next\tclaude plugin eval "{out}" --ablation none --runs 1 --threshold 0 --no-publish --trust-plugin --max-cost-usd <usd> --json <result.json>{grant}')


ERROR_KINDS = (
    ("turn_limit", r"maximum number of turns", "rerun these cases after build with a higher --max-turns"),
    ("timeout", r"timed out|timeout", "rerun once; if it repeats, raise timeout_seconds in the case"),
    ("api_error", r"api error|overloaded|rate.?limit|\b(?:429|5\d\d)\b", "rerun as is; the service failed, not the skill"),
    ("other", r"", "read the error before any rerun"),
)


def error_kind(error):
    return next(kind for kind, pattern, _ in ERROR_KINDS if re.search(pattern, error, re.I))


def fingerprint(root, whole):
    out = {}
    for path in sorted(Path(root).glob("skills/*/*.md")) if root and Path(root).is_dir() else []:
        rel, text = path.relative_to(root).as_posix(), path.read_text(encoding="utf-8", errors="replace")
        if whole and path.name in ("SKILL.md", "CONTRACT.md"):
            out[rel] = hashlib.sha256(text.encode()).hexdigest()[:16]
        elif not whole and path.name == "SKILL.md":
            fields = frontmatter(text)[0]
            listing = " ".join(filter(None, [fields.get("description"), fields.get("when_to_use")]))
            out[f"skill:{fields.get('name', path.parent.name)}"] = hashlib.sha256(listing.encode()).hexdigest()[:16]
    return out


def run_meta(data, whole):
    suite = data.get("suite") or {}
    return {"model": suite.get("modelOverride") or "default", "claude": data.get("claudeVersion"), "files": fingerprint(suite.get("root"), whole)}


def stale(now, then):
    if not then:
        return
    if then.get("model") != now["model"]:
        print(f"stale\tmodel\t{then.get('model')} -> {now['model']}\toutcomes are not comparable")
    if then.get("claude") != now["claude"]:
        print(f"stale\tclaude\t{then.get('claude')} -> {now['claude']}")
    old, new = then.get("files") or {}, now["files"]
    for key in sorted(set(old) | set(new)):
        if new and old.get(key) != new.get(key):
            print(f"stale\tchanged\t{key}")


def compare(now, accepted):
    good = ("hit", "ok")
    for key, outcome in now.items():
        before = accepted.get(key)
        if before is None:
            print(f"new\t{key}\t{outcome}")
        elif "unresolved" in (before, outcome):
            if before != outcome:
                print(f"not compared\t{key}\t{before} -> {outcome}")
        elif before != outcome:
            verdict = "gained" if outcome in good else "broken" if before in good else "changed"
            print(f"{verdict}\t{key}\t{before} -> {outcome}")


def by_split(outcomes, cases):
    split = {c["id"]: c.get("split", "write") for c in cases}
    if "test" not in split.values():
        return
    for name in ("write", "test"):
        mine = [o for key, o in outcomes.items() if split.get(key.split("#")[0], "write") == name and o != "unresolved"]
        positives = [o for o in mine if not (o == "ok" or o.startswith("false_fire"))]
        negatives = [o for o in mine if o == "ok" or o.startswith("false_fire")]
        fired = sum(o.startswith("false_fire") for o in negatives)
        print(f"split\t{name}\ttarget\t{positives.count('hit')}/{len(positives)}\tfalse_fires\t{fired}/{len(negatives)}")


def cmd_tally(a):
    outcomes = {}
    for file in a.results:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
        per = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
        false_fires, misses, runs, negatives = [], [], 0, 0
        reruns = collections.Counter()
        for case in data["cases"]:
            arm = case["arms"]["with"]
            for i, run in enumerate(arm):
                key = case["name"] if len(arm) == 1 else f"{case['name']}#{i + 1}"
                runs += 1
                graders = {g["name"]: g["passed"] for g in run["graders"]}
                took = sorted(n[3:] for n, ok in graders.items() if n.startswith("to-") and ok)
                spec = next((g.get("config") or {} for g in case["graders"] if g["name"] == "target"), {})
                m = re.search(r'\?([\w-]+)"$', spec.get("input_match", ""))
                expected = m.group(1) if m else spec["tool"] if spec.get("tool") not in (None, "Skill") else None
                error = str(run.get("error") or "")[:60]
                kind = error_kind(error) if error else ""
                if expected is None:
                    if error and not took:
                        reruns[kind] += 1
                        outcomes[key] = "unresolved"
                        misses.append(f"{case['name']} no skill expected -> unresolved {kind}, {error}")
                        continue
                    negatives += 1
                    outcomes[key] = f"false_fire:{','.join(took)}" if took else "ok"
                    if took:
                        false_fires.append(f"{case['name']} -> {','.join(took)}")
                    continue
                row = per[expected]
                row[0] += 1
                if graders.get("target") and graders.get("order", True):
                    row[1] += 1
                    outcomes[key] = "hit"
                elif graders.get("target"):
                    row[4] += 1
                    outcomes[key] = "wrong_order"
                    misses.append(f"{case['name']} {expected} -> called, but not after the step")
                elif took:
                    row[2] += 1
                    outcomes[key] = f"other:{','.join(took)}"
                    misses.append(f"{case['name']} {expected} -> {','.join(took)}")
                elif error:
                    row[3] += 1
                    reruns[kind] += 1
                    outcomes[key] = "unresolved"
                    misses.append(f"{case['name']} {expected} -> unresolved {kind}, {error}")
                else:
                    outcomes[key] = "none"
                    misses.append(f"{case['name']} {expected} -> none")
        total, hit = sum(r[0] for r in per.values()), sum(r[1] for r in per.values())
        print(f"file\t{file}\truns\t{runs}\tcost\t{data.get('costUsd', 0):.2f}\tpartial\t{data.get('partial')}")
        print(f"target\t{hit}/{total}\tfalse_fires\t{len(false_fires)}/{negatives}")
        print("skill\tcases\thit\tother_skill\tunresolved\tno_skill\twrong_order")
        for skill, (n, h, other, open_, late) in sorted(per.items()):
            print(f"{skill}\t{n}\t{h}\t{other}\t{open_}\t{n - h - other - open_ - late}\t{late}")
        for line in misses:
            print(f"miss\t{line}")
        for line in false_fires:
            print(f"false_fire\t{line}")
        for kind, _, action in ERROR_KINDS:
            if reruns[kind]:
                print(f"rerun\t{kind}\t{reruns[kind]}\t{action}")
    if a.cases:
        by_split(outcomes, json.loads(Path(a.cases).read_text(encoding="utf-8")))
    meta = run_meta(data, whole=False)
    if a.accepted:
        old = json.loads(Path(a.accepted).read_text(encoding="utf-8"))
        stale(meta, old.pop("_meta", None))
        compare(outcomes, old)
    if a.save:
        Path(a.save).write_text(json.dumps({**outcomes, "_meta": meta}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved\t{a.save}")


NOISE = 0.34


def cmd_quality(a):
    data = json.loads(Path(a.result).read_text(encoding="utf-8"))
    rows, breaks, cost, reruns = [], [], collections.Counter(), collections.Counter()
    for case in data["cases"]:
        defs = {g["name"]: g for g in case.get("graders", [])}
        withs, withouts = case["arms"].get("with", []), case["arms"].get("without", [])
        for arm, runs in (("with", withs), ("without", withouts)):
            cost[arm] += sum(r.get("costUsd") or 0 for r in runs)
        aborted = [r for r in withs + withouts if r.get("error")]
        for r in aborted:
            reruns[error_kind(str(r["error"]))] += 1
        withs, withouts = [r for r in withs if not r.get("error")], [r for r in withouts if not r.get("error")]
        indicator = {n for n, g in defs.items() if g.get("type") == "tool_used" and g.get("config", {}).get("tool") == "Skill"}

        def rate(runs, name):
            seen = [g["passed"] for r in runs for g in r["graders"] if g["name"] == name]
            return sum(seen) / len(seen) if seen else None
        fired = sum(any(g["passed"] for g in r["graders"] if g["name"] in indicator) for r in withs)
        fired = f"{fired}/{len(withs)}" if indicator else "-"
        print(f"case\t{case['name']}\tfired\t{fired}\tunresolved\t{len(aborted)}")
        for name in sorted(defs):
            if name in indicator:
                continue
            w, wo = rate(withs, name), rate(withouts, name)
            if w is None:
                continue
            flags = []
            if name.startswith("b-") and w < 1:
                flags.append("BOUNDARY")
                breaks.append(f"{case['name']}\t{name}\t{w:.2f}")
            if wo is not None and w == 1 and wo == 1:
                flags.append("nondiscriminating")
            if 0 < w < 1:
                flags.append("flaky")
            rows.append((case["name"], name, w, wo, flags))
    print(f"file\t{a.result}\tcost_with\t{cost['with']:.2f}\tcost_without\t{cost['without']:.2f}\tpartial\t{data.get('partial')}")
    for line in breaks:
        print(f"break\t{line}")
    for kind, _, action in ERROR_KINDS:
        if reruns[kind]:
            print(f"rerun\t{kind}\t{reruns[kind]}\t{action}")
    print("case\tgrader\twith\twithout\tdelta\tflags")
    for name, grader_name, w, wo, flags in rows:
        delta = "" if wo is None else f"{w - wo:+.2f}"
        print(f"{name}\t{grader_name}\t{w:.2f}\t{'' if wo is None else f'{wo:.2f}'}\t{delta}\t{','.join(flags)}")
    deltas = [w - wo for _, _, w, wo, _ in rows if wo is not None]
    print(f"summary\tgraders\t{len(rows)}\tboundary_breaks\t{len(breaks)}\tnondiscriminating\t{sum('nondiscriminating' in r[4] for r in rows)}"
          f"\tflaky\t{sum('flaky' in r[4] for r in rows)}\tmean_delta\t{sum(deltas) / len(deltas) if deltas else 0:+.2f}")
    now = {}
    for name, grader_name, w, _, _ in rows:
        now.setdefault(name, {})[grader_name] = w
    meta = run_meta(data, whole=True)
    if a.accepted:
        old = json.loads(Path(a.accepted).read_text(encoding="utf-8"))
        stale(meta, old.pop("_meta", None))
        for name, graders in now.items():
            for grader_name, w in graders.items():
                before = old.get(name, {}).get(grader_name)
                if before is None:
                    print(f"new\t{name}\t{grader_name}\t{w:.2f}")
                elif abs(w - before) > NOISE:
                    print(f"{'gained' if w > before else 'broken'}\t{name}\t{grader_name}\t{before:.2f} -> {w:.2f}")
    if a.save:
        Path(a.save).write_text(json.dumps({**now, "_meta": meta}, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"saved\t{a.save}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("summary", cmd_summary), ("check", cmd_check)):
        p = sub.add_parser(name)
        p.add_argument("plugin", type=plugin_root)
        p.add_argument("--usage")
        p.set_defaults(fn=fn)
    p = sub.add_parser("impact")
    p.add_argument("plugin", type=plugin_root)
    p.add_argument("node")
    p.set_defaults(fn=cmd_impact)
    p = sub.add_parser("sql")
    p.add_argument("plugin", type=plugin_root)
    p.add_argument("query")
    p.set_defaults(fn=cmd_sql)
    p = sub.add_parser("mine")
    p.add_argument("transcripts")
    p.add_argument("out")
    p.add_argument("--alias", action="append", default=[])
    p.add_argument("--only")
    p.set_defaults(fn=cmd_mine)
    p = sub.add_parser("catalog")
    p.add_argument("out")
    p.add_argument("dirs", nargs="+")
    p.add_argument("--only")
    p.set_defaults(fn=cmd_catalog)
    p = sub.add_parser("build")
    p.add_argument("plugin", type=plugin_root)
    p.add_argument("cases")
    p.add_argument("out")
    p.add_argument("--history")
    p.add_argument("--stub", action="store_true")
    p.add_argument("--also")
    p.add_argument("--max-turns", type=int, default=4)
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("tally")
    p.add_argument("results", nargs="+")
    p.add_argument("--cases", help="the cases.json the run was built from; cases with \"split\": \"test\" are scored apart")
    p.add_argument("--accepted", help="case outcomes saved from an accepted run, to report gained and broken")
    p.add_argument("--save", help="write this run's case outcomes, to accept it")
    p.set_defaults(fn=cmd_tally)
    p = sub.add_parser("quality")
    p.add_argument("result")
    p.add_argument("--accepted")
    p.add_argument("--save")
    p.set_defaults(fn=cmd_quality)
    a = parser.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
