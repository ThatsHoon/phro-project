"""Read a plugin's current structure and measure whether its skills get called. Standard library only.

Structure, rebuilt from the plugin's files on every call (the files are the source, the database is a throwaway copy):
  project.py summary <plugin> [--usage DIR]     nodes, and per skill its token cost, tests and use
  project.py check   <plugin> [--usage DIR]     deterministic findings, one per line
  project.py impact  <plugin> <node>            what depends on it, and what only it uses
  project.py sql     <plugin> "<select>"        read-only query: node(id, kind, path, description, desc_tokens, body_tokens), edge(src, dst, how)

Routing, on a scratch copy so the plugin itself is never edited:
  project.py mine    <transcripts_dir> <cases.json> [--alias a=b ...] [--only a,b]   prompts labelled by the command the user typed
  project.py catalog <out.json> <skills_dir> [<skills_dir> ...] [--only a,b]         competing descriptions from installed skills
  project.py build   <plugin> <cases.json> <out_dir> [--history f.jsonl] [--stub] [--also out.json] [--max-turns N]
  project.py tally   <result.json> [<result.json> ...]                              hits, misses, and which skill took each miss
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
LONG_BODY_LINES = 500
OVERLAP_MIN = 0.25  # ponytail: word Jaccard only finds candidates; semantic overlap is the model's call in audit
SHARED_LINES_MIN = 3
PATH_REF = re.compile(r"(?<![\w./-])((?:references|scripts|hooks|agents|skills|commands|assets|templates|fixtures)/[\w./-]*\w\.\w+)")
MD_LINK = re.compile(r"\]\(([^)#:\s<>]+\.\w+)\)")
IMPORT = re.compile(r"^\s*(?:from|import)\s+(\w+)", re.M)
STOP = set("the and for with use when this that from not you your any are can into only about all its it's run runs used using skill skills user users asks wants whenever even".split())
PROMPT_MIN, PROMPT_MAX = 10, 200  # shorter is a mode word, longer is usually pasted material
TOOLS = "[Read, Glob, Grep, WebSearch, Skill]"


def tokens(text):
    return len(text.encode("utf-8")) // 4  # ponytail: rough byte estimate; `claude plugin details` has the real listing cost


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
    if rel in ("hooks/hooks.json", ".mcp.json", ".claude-plugin/plugin.json"):
        return "config", rel
    suffix = Path(name).suffix
    if suffix in {".py", ".js", ".ts", ".tsx", ".cjs", ".mjs", ".sh", ".ps1"}:
        return "script", rel
    return ("doc" if suffix in TEXT else "asset"), rel


def scan(root):
    """Return (database, plugin name, {node id: text}, {node id: source files})."""
    root = Path(root).resolve()
    manifest_path = root / ".claude-plugin/plugin.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    plugin = manifest.get("name", root.name)
    db = sqlite3.connect(":memory:")
    db.executescript("""
        create table node(id text primary key, kind text, path text, description text, desc_tokens int, body_tokens int);
        create table edge(src text, dst text, how text, primary key(src, dst, how));""")
    texts, sources, kinds = collections.defaultdict(str), collections.defaultdict(list), {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if not path.is_file() or SKIP & set(rel.split("/")):
            continue
        kind, node = classify(rel)
        kinds[node] = kind
        sources[node].append(rel)
        if path.suffix in TEXT:
            texts[node] += path.read_text(encoding="utf-8", errors="replace") + "\n"
    # MCP servers are nodes of their own, so a server's script has a dependent.
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
        db.execute("insert into node values(?,?,?,?,?,?)", (node, kind, where, listing, tokens(listing), tokens(body)))
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
            graded = kinds[src] == "eval" and re.search(r"(?m)^.*(?:input_match|skill).*(?<![\w-])" + re.escape(name) + r"(?![\w-]).*$", text)
            if called or graded:
                db.execute("insert or ignore into edge values(?,?,'name')", (src, node))


def usage(directory, plugin):
    """Typed commands and model Skill calls per skill. Forked sessions copy lines, so every event counts once by uuid. Counts only, no text."""
    typed, model, seen = collections.Counter(), collections.Counter(), set()
    for path in Path(directory).rglob("*.jsonl"):
        for line in path.open(encoding="utf-8", errors="replace"):
            if '"Skill"' not in line and "<command-name>" not in line:
                continue
            try:
                e = json.loads(line)
            except ValueError:
                continue
            uid = e.get("uuid")
            if uid in seen or e.get("isSidechain"):
                continue
            seen.add(uid)
            content = (e.get("message") or {}).get("content")
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
    return lambda name: (count(typed, name), count(model, name))


def findings(root, usage_dir=None):
    root = Path(root).resolve()
    db, plugin, texts, sources = scan(root)
    out = []
    nodes = db.execute("select id, kind, description, desc_tokens from node").fetchall()
    kinds = {n[0]: n[1] for n in nodes}
    # Broken references, in the files that run. Docs and tests may mention what no longer exists on purpose.
    every = [rel for files in sources.values() for rel in files]
    instructions = {rel for node, files in sources.items() if kinds[node] in ("skill", "agent", "command", "config") for rel in files}
    for rel in every:
        # Reference documents quote examples of other projects' files, so only files that are followed directly count.
        scripted = rel.startswith(("skills/", "scripts/", "hooks/")) and Path(rel).suffix in {".py", ".js", ".ts", ".tsx", ".cjs", ".mjs", ".sh", ".ps1"}
        if rel not in instructions and not scripted:
            continue
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        named = set(PATH_REF.findall(text))
        links = set(MD_LINK.findall(text)) if rel.endswith(".md") else set()
        for ref in sorted(named | links):
            # A script's comment may name a skill's file relative to that skill, so any file ending in the path counts.
            found = (root / ref).exists() or ((root / rel).parent / ref).exists() or any(f.endswith("/" + ref) for f in every)
            # A link into a folder this plugin does not have is an example about the user's project, not a reference.
            head = ref.removeprefix("./").split("/")[0]
            example = ref not in named and "/" in ref and not (root / head).exists() and not ((root / rel).parent / head).exists()
            if not found and not example:
                out.append(("broken-ref", rel, ref))
    # Whole-file duplicates, then lines copied between instruction files.
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
    # Parts nothing reaches.
    reached = {r[0] for r in db.execute("select distinct dst from edge")}
    for node, kind in kinds.items():
        path = sources[node][0] if sources[node] else ""
        runs = path.startswith(("skills/", "scripts/", "hooks/")) and (kind == "script" or (kind == "doc" and path.endswith(".md")))
        if runs and node not in reached and Path(path).name not in ("__init__.py", "README.md"):
            out.append(("orphan", node, "no file refers to it"))
    tested = {r[0] for r in db.execute("select e.dst from edge e join node n on n.id = e.src where n.kind = 'eval'")}
    words = {}
    for node, kind, description, _ in nodes:
        if kind != "skill":
            continue
        if node not in tested:
            out.append(("no-eval", node, "no eval case grades it"))
        if not description:
            out.append(("no-description", node, "the model cannot pick it from the listing"))
        elif len(description) > LISTING_LIMIT:
            out.append(("long-description", node, f"{len(description)} chars, cut at {LISTING_LIMIT}"))
        if frontmatter(texts[node])[1].count("\n") > LONG_BODY_LINES:
            out.append(("long-body", node, f"over {LONG_BODY_LINES} lines"))
        words[node] = {w for w in re.findall(r"\w{3,}", description.lower()) if w not in STOP}
    skills = sorted(words)
    for i, a in enumerate(skills):
        for b in skills[i + 1:]:
            union = words[a] | words[b]
            score = len(words[a] & words[b]) / len(union) if union else 0
            if score >= OVERLAP_MIN:
                out.append(("overlap-candidate", a, f"{b}: {score:.2f}"))
    if usage_dir:
        used = usage(usage_dir, plugin)
        out += [("never-called", s, "0 typed, 0 model calls in these transcripts") for s in skills if used(s.split(":", 1)[1]) == (0, 0)]
    return out


def resolve(db, name):
    hits = [r[0] for r in db.execute("select id from node") if r[0] == name or r[0].endswith(":" + name) or r[0].endswith("/" + name)]
    if len(hits) != 1:
        sys.exit(f"node {name!r}: {len(hits)} matches {hits[:5]}")
    return hits[0]


def cmd_summary(a):
    db, plugin, _, _ = scan(a.plugin)
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


def cmd_check(a):
    out = findings(a.plugin, a.usage)
    for row in out:
        print("\t".join(row))
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
    print(f"cases\t{len(cases)}")  # the prompts stay in the file; they are the user's own words
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
                out.setdefault(name, text[:LISTING_LIMIT])  # the newest cached version is not guaranteed; pass one directory per plugin if it matters
    Path(a.out).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"catalog\t{len(out)}")


def grader(name_pattern=None, none=False):
    match = f"input_match: '\"skill\"\\s*:\\s*\"(?:[\\w-]+:)?{name_pattern}\"'\n" if name_pattern else ""
    limit = "min: 0\nmax: 0\n" if none else ""
    return f"---\ntype: tool_used\ntool: Skill\n{match}{limit}---\n\nGraded from the transcript.\n"


def cmd_build(a):
    src, out = Path(a.plugin).resolve(), Path(a.out).resolve()
    if out == src or src in out.parents:
        sys.exit("out_dir must be outside the plugin")
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(src, out, ignore=shutil.ignore_patterns(*SKIP, "evals", "tests"))
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
        (d / "prompt.md").write_text(f"---\nmax_turns: {a.max_turns}\ntimeout_seconds: 180\nallowed_tools: {TOOLS}\n---\n\n{case['prompt']}\n", encoding="utf-8")
        target = case.get("skill")
        (d / "graders/target.md").write_text(grader(target) if target else grader(none=True), encoding="utf-8")
        for name in skills:  # free graders, so a miss shows which skill took it
            (d / f"graders/to-{name}.md").write_text(grader(name), encoding="utf-8")
        if a.history:
            shutil.copy(a.history, d / "history.jsonl")
            (d / "case.yaml").write_text(f"schema_version: \"1.1\"\nname: {case['id']}\ncontext:\n  history_file: history.jsonl\n", encoding="utf-8")
    print(f"built\t{out}\tskills\t{len(skills)}\tcases\t{len(cases)}")
    print(f'next\tclaude plugin eval "{out}" --ablation none --runs 1 --threshold 0 --no-publish --trust-plugin --max-cost-usd <usd> --json <result.json>')


def cmd_tally(a):
    for file in a.results:
        data = json.loads(Path(file).read_text(encoding="utf-8"))
        per = collections.defaultdict(lambda: [0, 0, 0, 0])
        false_fires, misses, runs = [], [], 0
        for case in data["cases"]:
            for run in case["arms"]["with"]:
                runs += 1
                graders = {g["name"]: g["passed"] for g in run["graders"]}
                took = sorted(n[3:] for n, ok in graders.items() if n.startswith("to-") and ok)
                pattern = next((g["config"].get("input_match", "") for g in case["graders"] if g["name"] == "target"), "")
                expected = m.group(1) if (m := re.search(r'\?([\w-]+)"$', pattern)) else None
                if expected is None:
                    if took:
                        false_fires.append(f"{case['name']} -> {','.join(took)}")
                    continue
                row = per[expected]
                row[0] += 1
                # A run stopped by the turn limit with no skill yet has not missed; it is unresolved.
                cut = "maximum number of turns" in str(run.get("error"))
                if graders.get("target"):
                    row[1] += 1
                elif took:
                    row[2] += 1
                    misses.append(f"{case['name']} {expected} -> {','.join(took)}")
                elif cut:
                    row[3] += 1
                    misses.append(f"{case['name']} {expected} -> unresolved, cut off")
                else:
                    misses.append(f"{case['name']} {expected} -> none")
        total, hit = sum(r[0] for r in per.values()), sum(r[1] for r in per.values())
        print(f"file\t{file}\truns\t{runs}\tcost\t{data.get('costUsd', 0):.2f}\tpartial\t{data.get('partial')}")
        print(f"target\t{hit}/{total}")
        print("skill\tcases\thit\tother_skill\tcut_off\tno_skill")
        for skill, (n, h, other, cut) in sorted(per.items()):
            print(f"{skill}\t{n}\t{h}\t{other}\t{cut}\t{n - h - other - cut}")
        for line in misses:
            print(f"miss\t{line}")
        for line in false_fires:
            print(f"false_fire\t{line}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, fn in (("summary", cmd_summary), ("check", cmd_check)):
        p = sub.add_parser(name)
        p.add_argument("plugin")
        p.add_argument("--usage")
        p.set_defaults(fn=fn)
    p = sub.add_parser("impact")
    p.add_argument("plugin")
    p.add_argument("node")
    p.set_defaults(fn=cmd_impact)
    p = sub.add_parser("sql")
    p.add_argument("plugin")
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
    p.add_argument("plugin")
    p.add_argument("cases")
    p.add_argument("out")
    p.add_argument("--history")
    p.add_argument("--stub", action="store_true")
    p.add_argument("--also")
    p.add_argument("--max-turns", type=int, default=4)  # at 2, models that read files first were cut off before any skill call
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("tally")
    p.add_argument("results", nargs="+")
    p.set_defaults(fn=cmd_tally)
    a = parser.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
