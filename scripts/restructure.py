#!/usr/bin/env python3
"""Map a project's files and dependencies, check a move plan, apply it with reference rewrites, verify nothing was lost, undo it. Standard library only. Prints paths and counts, never file contents.

Move plan, moves.json: {"moves": {"old/path": "new/path"}, "also": ["a folder outside the project whose mentions of project paths follow the move"]}
"""
import argparse
import ast
import collections
import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning)  # parsing other people's code should not print about it

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".phro-project", ".idea", ".vscode", "site-packages", ".cache"}
SCRIPT = {".py", ".js", ".ts", ".tsx", ".jsx", ".cjs", ".mjs", ".sh", ".ps1", ".bat", ".cmd"}
JS = {".js", ".ts", ".tsx", ".jsx", ".cjs", ".mjs", ".vue", ".svelte"}
WEB = {".html", ".htm", ".css", ".vue", ".svelte"}
DOC = {".md", ".txt", ".rst"}
CONFIG = {".json", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
TEXT = SCRIPT | WEB | DOC | CONFIG
MAX_READ = 1_500_000
MAX_LINE = 2000
BULK_FILES = 200  # a folder with this many files and almost no code is data, moved whole and not read
HARD = {"import", "js", "path"}
SHOWN = 12
STATE = ".phro-project/restructure"

QUOTED = re.compile(r"""(["'`])((?:(?!\1)[^\n]){2,240})\1""")
MD_LINK = re.compile(r"\]\(([^)\s]+)\)")
BARE = re.compile(r"(?<![\w.\-/\\])((?:\.{1,2}[/\\]+)*[\w가-힣.\-]+(?:[/\\]+[\w가-힣.\-]+)+|[\w가-힣\-]+\.[A-Za-z][A-Za-z0-9]{0,5})(?![\w\-])")
ENV_PREFIX = re.compile(r"(?:\$\{?\w+\}?|%\w+%)[/\\]+")
TAIL = re.compile(r"(?::\d+(?:-\d+)?|#[^/]*)$")
FRAGILE = {
    "sys-path": re.compile(r"sys\.path\.(?:insert|append)"),
    "parents": re.compile(r"parents\[\d+\]|\.parent\.parent"),
    "file-dir": re.compile(r"__file__|__dirname|\$PSScriptRoot|BASH_SOURCE"),
    "home": re.compile(r"Path\.home\(\)|expanduser\("),
    "abs-path": re.compile(r"""["'][A-Za-z]:[\\/]|["']/(?:home|Users)/"""),
    "path-join": re.compile(r"""os\.path\.join\(|\s/\s*["'][\w.]|path\.join\(|path\.resolve\("""),
    "cwd": re.compile(r"os\.getcwd\(\)|Path\.cwd\(\)|os\.chdir\(|process\.cwd\(\)"),
}
BACKUP_NAME = re.compile(r"(^|[_\-. ])(backup|bak|old|orig|copy|복사본|백업)([_\-. \d]|$)", re.I)
BACKUP_FILE = re.compile(r"\.(bak|orig|old)(_|$|\.)|~$|\.bak_", re.I)
LOGIC_SKIP = re.compile(r"""^\s*(?:from\s+\S+\s+import\b|import\s+[\w.,\s]+(?:as\s+\w+)?\s*$|import\s.*\sfrom\s|const\s.*require\(|sys\.path\.|#|//|$)""")


def is_test(rel):
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or ".test." in name or "_test." in name or any(p in ("tests", "test", "__tests__") for p in rel.split("/")[:-1])


def sha(data):
    return hashlib.sha256(data).hexdigest()[:16]


def logic_sha(text):
    """A file's hash without import lines, comments and blanks: it moves only when behaviour may have changed."""
    return sha("\n".join(line.strip() for line in text.splitlines() if not LOGIC_SKIP.match(line)).encode("utf-8"))


def read_text(path):
    return path.read_bytes().decode("utf-8")


def dotted(rel):
    """The module name of a project path from the root, or None when a folder name cannot be imported."""
    parts = rel.split("/")
    if parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts and all(p.isidentifier() for p in parts) else None


def check_also(root, also):
    """A move here rewrites the text under --also, so folders shared by every project and other projects' folders are refused."""
    claude = Path.home().resolve() / ".claude"
    projects = claude / "projects"
    mine = projects / re.sub(r"[^A-Za-z0-9]", "-", str(Path(root).resolve()))
    for folder in also:
        shared = folder in (claude, projects, claude / "scheduled-tasks") or folder in claude.parents
        other = projects in folder.parents and folder != mine and mine not in folder.parents
        if shared or other:
            sys.exit(f"--also {folder}: shared with other projects. Name this project's memory folder or one task's folder.")


class Scan:
    """The project as read now: files, folders, and every reference that resolves to one of them."""

    def __init__(self, root, also=()):
        self.root = Path(root).resolve()
        self.also = [Path(a).expanduser().resolve() for a in also]
        check_also(self.root, self.also)
        self.files, self.dirs, self.bulk = {}, set(), {}
        self.edges, self.fragile, self.undecodable = [], [], []
        self.suffix = collections.defaultdict(list)
        self.mains = set()
        self.logic = {}
        self.hashes = {}
        parts = self.root.as_posix().split("/")
        self.root_prefix = re.compile(r"[\\/]+".join(map(re.escape, parts)) + r"[\\/]+", re.I)
        self.walk()
        for rel in self.files:
            if rel.endswith(".py"):
                pieces = rel[:-3].split("/")
                if pieces[-1] == "__init__":
                    pieces = pieces[:-1]
                for i in range(len(pieces)):
                    self.suffix[".".join(pieces[i:])].append(rel)
        for rel, (kind, size) in list(self.files.items()):
            if Path(rel).suffix.lower() in TEXT and size <= MAX_READ:
                self.read_file(rel)
        for folder in self.also:
            for path in sorted(folder.rglob("*")):
                if path.is_file() and path.suffix.lower() in TEXT and path.stat().st_size <= MAX_READ:
                    self.read_also(path)

    def walk(self):
        for folder, dirs, names in os.walk(self.root):
            here = Path(folder)
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not (here / d / "pyvenv.cfg").exists())
            rel_dir = here.relative_to(self.root).as_posix()
            rel_dir = "" if rel_dir == "." else rel_dir
            if rel_dir:
                self.dirs.add(rel_dir)
            code = sum(Path(n).suffix.lower() in SCRIPT for n in names)
            if len(names) >= BULK_FILES and code * 10 < len(names):
                self.bulk[rel_dir] = len(names)
                continue
            for name in names:
                path = here / name
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                suffix = path.suffix.lower()
                kind = "code" if suffix in SCRIPT else "web" if suffix in WEB else "doc" if suffix in DOC else "config" if suffix in CONFIG else "data"
                self.files[f"{rel_dir}/{name}" if rel_dir else name] = (kind, size)

    def known(self, rel):
        if rel in self.files or rel in self.dirs:
            return True
        parent = rel
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in self.bulk:
                return (self.root / rel).exists()
        return False

    def edge(self, src, dst, how, line, start, end, token, form, **extra):
        self.edges.append({"src": src, "dst": dst, "how": how, "line": line, "start": start, "end": end, "token": token, "form": form, **extra})

    def read_file(self, rel):
        path = self.root / rel
        try:
            data = path.read_bytes()
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            self.undecodable.append(rel)
            return
        except OSError:
            return
        self.hashes[rel] = sha(data)
        suffix = path.suffix.lower()
        if suffix in SCRIPT:
            self.logic[rel] = logic_sha(text)
        if suffix == ".py":
            self.python_imports(rel, text)
        if "__main__" in text or text.startswith("#!"):
            self.mains.add(rel)
        is_js = suffix in JS or suffix in WEB
        for number, line in enumerate(text.split("\n"), 1):
            if len(line) > MAX_LINE:
                continue
            if suffix in SCRIPT:
                for kind, rx in FRAGILE.items():
                    if rx.search(line):
                        self.fragile.append([rel, kind, number])
            self.tokens(rel, number, line, is_js)

    def read_also(self, path):
        try:
            text = read_text(path)
        except (UnicodeDecodeError, OSError):
            return
        for number, line in enumerate(text.split("\n"), 1):
            if len(line) <= MAX_LINE:
                self.tokens(f"also:{path.as_posix()}", number, line, False)

    def mask(self, line):
        blank = lambda m: " " * len(m.group(0))
        return ENV_PREFIX.sub(blank, self.root_prefix.sub(blank, line))

    def tokens(self, src, number, line, is_js):
        masked = self.mask(line)
        taken = []

        def consider(start, end):
            raw = masked[start:end]
            start += len(raw) - len(raw.lstrip())
            raw = raw.strip()
            found = TAIL.search(raw)
            if found:
                raw = raw[:found.start()]
            raw = raw.rstrip("/\\")
            end = start + len(raw)
            if not raw or "://" in raw or raw[0] in "~#@" or any(c in raw for c in '<>*?|{}$"\' '.replace(" ", "")):
                return
            prefixed = start > 0 and masked[start - 1] == " " and line[start - 1] in "/\\"
            hit = self.resolve(src, raw.replace("\\\\", "/").replace("\\", "/"), is_js, prefixed)
            if hit:
                self.edge(src, hit[0], hit[1], number, start, end, raw, hit[2])
                taken.append((start, end))

        for found in QUOTED.finditer(masked):
            consider(found.start(2), found.end(2))
        for found in MD_LINK.finditer(masked):
            if not any(a <= found.start(1) < b for a, b in taken):
                consider(found.start(1), found.end(1))
        for found in BARE.finditer(masked):
            if not any(a < found.end(1) and found.start(1) < b for a, b in taken):
                consider(found.start(1), found.end(1))

    def resolve(self, src, token, is_js, prefixed=False):
        """(target, how, form) for a path-like token, or None. A token only counts when the target exists, which is what keeps this precise."""
        if prefixed and self.known(posixpath.normpath(token)):
            return posixpath.normpath(token), "path", "root"
        if src.startswith("also:"):
            return (token, "path", "root") if "/" in token and self.known(token) else None
        here = posixpath.dirname(src)
        relative = token.startswith(("./", "../"))
        if relative or "/" in token or "." in token.rsplit("/", 1)[-1]:
            joined = posixpath.normpath(posixpath.join(here, token))
            if not joined.startswith("..") and joined != ".":
                if self.known(joined):
                    return joined, "path", "rel"
                if relative and is_js:
                    for ext in (".js", ".ts", ".tsx", ".jsx", ".cjs", ".mjs", ".json", "/index.js", "/index.ts"):
                        if joined + ext in self.files:
                            return joined + ext, "js", "js"
        if relative:
            return None
        if "/" in token and self.known(posixpath.normpath(token)):
            return posixpath.normpath(token), "path", "root"
        if "/" not in token and "." in token and len(token) > 5 and Path(src).suffix.lower() in SCRIPT:
            same = [f for f in self.files if f.endswith("/" + token) or f == token] if token not in self.name_index() else self.name_index()[token]
            if len(same) == 1:
                return same[0], "name", "name"
        return None

    def name_index(self):
        if not hasattr(self, "_names"):
            names = collections.defaultdict(list)
            for rel in self.files:
                names[rel.rsplit("/", 1)[-1]].append(rel)
            self._names = names
        return self._names

    def python_imports(self, rel, text):
        try:
            tree = ast.parse(text)
        except (SyntaxError, ValueError):
            return
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.python_edge(rel, node.lineno, "import", alias.name, None, alias.asname, 0)
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    self.python_edge(rel, node.lineno, "from", node.module or "", alias.name, alias.asname, node.level)

    def module_at(self, base, dotted_name):
        path = posixpath.normpath(posixpath.join(base, dotted_name.replace(".", "/"))) if dotted_name else (base or ".")
        if path + ".py" in self.files:
            return path + ".py"
        if path + "/__init__.py" in self.files:
            return path + "/__init__.py"
        return path if path in self.dirs else None

    def python_edge(self, rel, line, stmt, module, name, alias, level):
        here = posixpath.dirname(rel)
        if level:
            base = here
            for _ in range(level - 1):
                base = posixpath.dirname(base)
            bases = [("rel", base)]
        else:
            bases = [("dir", here), ("root", "")]
        name_path = f"{module}.{name}" if module and name and name != "*" else (name if name and name != "*" else None)
        for kind, base in bases:
            if name_path:
                dst = self.module_at(base, name_path)
                if dst:
                    return self.edge(rel, dst, "import", line, 0, 0, name_path, "py", stmt=stmt, module=module, name=name, alias=alias, level=level, base=kind, via="name")
            if module:
                dst = self.module_at(base, module)
                if dst:
                    return self.edge(rel, dst, "import", line, 0, 0, module, "py", stmt=stmt, module=module, name=name, alias=alias, level=level, base=kind, via="module")
        top = module.split(".")[0] if module else ""
        if not level and top not in sys.stdlib_module_names:
            found = self.suffix.get(module, [])
            if len(found) == 1 and found[0] != rel:
                self.edge(rel, found[0], "import~", line, 0, 0, module, "py", stmt=stmt, module=module, name=name, alias=alias, level=level, base="hack", via="module")


def top(rel):
    return rel.split("/", 1)[0] if "/" in rel else "."


def clusters(scan):
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    code = {f for f, (kind, _) in scan.files.items() if kind in ("code", "web")}
    for e in scan.edges:
        real = e["how"] in ("import", "import~", "js") or (e["how"] == "path" and scan.files.get(e["src"], ("",))[0] == "web")  # a path string in a script may be a comment
        if real and e["src"] in code and e["dst"] in code and e["src"] != e["dst"]:
            parent[find(e["src"])] = find(e["dst"])
    groups = collections.defaultdict(list)
    for f in code:
        groups[find(f)].append(f)
    return sorted(groups.values(), key=len, reverse=True)


def smells(scan):
    out = collections.defaultdict(list)
    hashes = collections.defaultdict(list)
    for rel, value in scan.hashes.items():
        if scan.files[rel][1] > 200:
            hashes[value].append(rel)
    for group in hashes.values():
        if len({posixpath.dirname(g) for g in group}) > 1:
            out["duplicate"].append(" = ".join(sorted(group)[:3]) + (f" (+{len(group) - 3})" if len(group) > 3 else ""))
    for folder in sorted(scan.dirs):
        if BACKUP_NAME.search(folder.rsplit("/", 1)[-1]):
            out["backup-folder"].append(folder)
    inbound = collections.Counter(e["dst"] for e in scan.edges if e["how"] in HARD | {"import~"} and e["src"] != e["dst"])
    for rel, (kind, _) in scan.files.items():
        parts = rel.split("/")
        if BACKUP_FILE.search(parts[-1]):
            out["backup-file"].append(rel)
        if kind == "code" and "docs" in parts[:-1]:
            out["code-in-docs"].append(rel)
        if kind == "code" and len(parts) > 6:
            out["deep-code"].append(rel)
        if kind == "code" and not inbound[rel] and rel not in scan.mains and not is_test(rel) and parts[-1] != "__init__.py":
            out["orphan-code"].append(rel)
    for rel, (kind, _) in scan.files.items():
        if "/" not in rel and Path(rel).suffix.lower() in {".zip", ".tar", ".gz", ".7z"}:
            out["archive-at-root"].append(rel)
    for rel in scan.undecodable:
        out["not-utf8"].append(rel)
    return out


def summary(scan):
    sizes, counts, code = collections.Counter(), collections.Counter(), collections.Counter()
    for rel, (kind, size) in scan.files.items():
        sizes[top(rel)] += size
        counts[top(rel)] += 1
        code[top(rel)] += kind == "code"
    for folder, n in scan.bulk.items():
        counts[top(folder or ".")] += n
    print("TOP\tfiles\tcode\tMB")
    for name, n in counts.most_common():
        print(f"{name}\t{n}\t{code[name]}\t{sizes[name] // 1_000_000}")
    print(f"BULK\t{len(scan.bulk)} folders held as data\t" + ", ".join(f"{k or '.'} ({v})" for k, v in sorted(scan.bulk.items(), key=lambda x: -x[1])[:5]))
    tests = [f for f in scan.files if is_test(f) and scan.files[f][0] == "code"]
    print(f"TESTS\t{len(tests)}\t" + ", ".join(sorted(tests)[:5]))
    roots = sorted(posixpath.dirname(f) for f in scan.files if f.rsplit("/", 1)[-1] in {"package.json", "pyproject.toml", "requirements.txt", "setup.py"} and "/" in f)
    print(f"SUBPROJECTS\t{len(roots)}\t" + ", ".join(roots[:SHOWN]))
    print("\nSMELL\tcount\texamples")
    for kind, rows in sorted(smells(scan).items(), key=lambda x: -len(x[1])):
        print(f"{kind}\t{len(rows)}\t" + " | ".join(rows[:3]))
    fragile = collections.Counter(row[1] for row in scan.fragile)
    print("\nFRAGILE\t" + ", ".join(f"{k} {v}" for k, v in fragile.most_common()))
    groups = clusters(scan)
    print(f"\nCLUSTER\tfiles\tfolders ({len(groups)} groups, by real references)")
    for group in groups[:SHOWN]:
        folders = collections.Counter(posixpath.dirname(f) or "." for f in group)
        print(f"-\t{len(group)}\t" + ", ".join(f"{k} ({v})" for k, v in folders.most_common(3)))
    inbound = collections.Counter(e["dst"] for e in scan.edges if e["how"] in {"import", "import~", "js"} and top(e["src"]) != top(e["dst"]))
    print("\nHUB\tused across folders\tfile")
    for rel, n in inbound.most_common(SHOWN):
        print(f"-\t{n}\t{rel}")
    cross = collections.Counter((top(e["src"]), top(e["dst"])) for e in scan.edges if e["how"] in {"import", "import~", "js"} and top(e["src"]) != top(e["dst"]))
    print("\nCROSS\tcount\tfrom -> to (both directions mean a cycle)")
    for (a, b), n in cross.most_common(SHOWN):
        print(f"{'cycle' if (b, a) in cross else '-'}\t{n}\t{a} -> {b}")


def load_plan(path):
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    moves = {k.rstrip("/"): v.rstrip("/") for k, v in plan["moves"].items()}
    return moves, plan.get("also", [])


def mapper(moves):
    ordered = sorted(moves, key=len, reverse=True)

    def mapped(rel):
        for old in ordered:
            if rel == old or rel.startswith(old + "/"):
                return moves[old] + rel[len(old):]
        return rel
    return mapped


def validate(root, moves):
    errors = []
    root = Path(root)
    targets = collections.defaultdict(list)
    for old, new in moves.items():
        if posixpath.isabs(old) or posixpath.isabs(new) or ":" in new or ".." in new.split("/") or ".." in old.split("/"):
            errors.append(f"not inside the project: {old} -> {new}")
            continue
        if not (root / old).exists():
            errors.append(f"source missing: {old}")
            continue
        if new == old or new.startswith(old + "/"):
            errors.append(f"target inside its own source: {old} -> {new}")
            continue
        targets[new.lower()].append(old)
        target = root / new
        if target.exists():
            if target.is_dir() and (root / old).is_dir():
                for folder, _, names in os.walk(root / old):
                    for name in names:
                        rel = (Path(folder) / name).relative_to(root / old)
                        if (target / rel).exists() and not any((new + "/" + rel.as_posix()) == o or (new + "/" + rel.as_posix()).startswith(o + "/") for o in moves):
                            errors.append(f"target exists: {new}/{rel.as_posix()}")
            elif not any(new == o or new.startswith(o + "/") for o in moves):
                errors.append(f"target exists: {new}")
    errors += [f"same target: {' and '.join(olds)}" for olds in targets.values() if len(olds) > 1]
    return errors


def between(target, start):
    """A relative path inside the project, independent of the working folder."""
    return posixpath.relpath("/" + target, "/" + start)


def same_position(edge, src, dst):
    return between(dst, posixpath.dirname(src)) == between(edge["dst"], posixpath.dirname(edge["src"]))


def new_token(edge, mapped):
    """The replacement for a path token after the moves, or None when it stays right. A token that still points the same way keeps its spelling."""
    src, dst = mapped(edge["src"]), mapped(edge["dst"])
    old = edge["token"]
    plain = old.replace("\\\\", "/").replace("\\", "/")
    if edge["form"] == "root":
        if dst == edge["dst"]:
            return None
        new = dst
    else:
        if edge["src"].startswith("also:") or same_position(edge, src, dst):
            return None
        new = between(dst, posixpath.dirname(src))
        if edge["form"] == "js":
            if not plain.endswith(Path(edge["dst"]).suffix) and new.endswith(Path(edge["dst"]).suffix):
                new = new[: -len(Path(edge["dst"]).suffix)]
            if plain.rsplit("/", 1)[-1] != "index" and new.endswith("/index") and not edge["dst"].endswith(plain + Path(edge["dst"]).suffix):
                new = new[: -len("/index")]
            if not new.startswith("."):
                new = "./" + new
        elif plain.startswith("./") and not new.startswith("."):
            new = "./" + new
    if new == posixpath.normpath(plain) or new == plain:
        return None
    if "\\\\" in old:
        return new.replace("/", "\\\\")
    return new.replace("/", "\\") if "\\" in old else new


def import_change(edge, mapped):
    """(old module text, new module text, form) for a Python import, or a string with the reason it needs a person. None when it stays right."""
    src, dst = mapped(edge["src"]), mapped(edge["dst"])
    base = edge["base"]
    if src == edge["src"] and dst == edge["dst"]:
        return None
    if base in ("dir", "rel", "hack") and same_position(edge, src, dst):  # a whole subproject moved: its own import root moved with it
        return None
    if base in ("root", "hack") and dst == edge["dst"]:
        return None
    module = dotted(dst)
    if module is None:
        return f"a folder in {dst} cannot be imported"
    stem = module.rsplit(".", 1)[-1]
    package = module.rsplit(".", 1)[0] if "." in module else ""
    written = "." * edge["level"] + edge["module"]
    if edge["stmt"] == "from":
        if edge["via"] == "module":
            return written, module, "from"
        if stem != edge["name"]:
            return f"module renamed, {edge['name']} is now {stem}"
        if not package:
            return f"{stem} moved to the root, write the import by hand"
        return written, package, "from"
    if edge["alias"]:
        return edge["module"], module, "import-as"
    if "." in edge["module"]:
        return "dotted import without alias, its uses must change too"
    if module == edge["module"]:
        return None
    if stem != edge["module"]:
        return f"module renamed, {edge['module']} is now {stem}"
    return edge["module"], (package, stem), "import-bare"


def rewrite_import(line, change):
    old, new, form = change
    if form == "from":
        done, count = re.subn(rf"^(\s*from\s+){re.escape(old)}(\s+import\b)", lambda m: m.group(1) + new + m.group(2), line)
        if count:
            return done
        return line if re.match(rf"^\s*from\s+{re.escape(new)}\s+import\b", line) else None
    if form == "import-as":
        done, count = re.subn(rf"^(\s*import\s+){re.escape(old)}(\s+as\b)", lambda m: m.group(1) + new + m.group(2), line)
        return done if count else None
    package, stem = new
    done, count = re.subn(rf"^(\s*)import\s+{re.escape(old)}(\s*(?:#.*)?)$", lambda m: f"{m.group(1)}from {package} import {stem}{m.group(2)}", line.rstrip("\r"))
    return done + ("\r" if line.endswith("\r") else "") if count else None


def compute(scan, moves):
    """Per-file edits for a plan: {src: {line: {"spans": [...], "imports": [...]}}}, plus what a person must fix."""
    mapped = mapper(moves)
    edits = collections.defaultdict(lambda: collections.defaultdict(lambda: {"spans": [], "imports": []}))
    manual = []
    for e in scan.edges:
        if e["how"] == "path" or e["how"] == "js":
            new = new_token(e, mapped)
            if new is not None:
                edits[e["src"]][e["line"]]["spans"].append((e["start"], e["end"], new))
        elif e["how"] in ("import", "import~"):
            change = import_change(e, mapped)
            if isinstance(change, str):
                manual.append((e["src"], e["line"], e["token"], change))
            elif change:
                if e["how"] == "import~":
                    manual.append((e["src"], e["line"], e["token"], f"found only through a path hack, should import {dotted(mapped(e['dst']))}"))
                else:
                    edits[e["src"]][e["line"]]["imports"].append((change, e["token"]))
        elif posixpath.dirname(mapped(e["src"])) != posixpath.dirname(mapped(e["dst"])) and mapped(e["dst"]) != e["dst"]:
            manual.append((e["src"], e["line"], e["token"], f"name only, the file is now at {mapped(e['dst'])}"))
    return edits, manual, fragile_lines(scan, moves, mapped)


def fragile_lines(scan, moves, mapped):
    """Code that computes paths and may break silently: depth-sensitive lines in moved files, and any such line that names a moved folder."""
    words = {part for key in moves for part in key.split("/") if len(part) > 1}
    named = re.compile(r"(?<![\w\-])(?:" + "|".join(map(re.escape, sorted(words, key=len, reverse=True))) + r")(?![\w\-])") if words else None
    out, texts = [], {}
    for rel, kind, number in scan.fragile:
        moved = mapped(rel) != rel
        if moved and kind in ("sys-path", "parents", "file-dir"):
            out.append([rel, kind, number])
            continue
        if rel not in texts:
            try:
                texts[rel] = read_text(scan.root / rel).split("\n")
            except (OSError, UnicodeDecodeError):
                texts[rel] = []
        line = texts[rel][number - 1] if number <= len(texts[rel]) else ""
        if named and named.search(line):
            out.append([rel, kind, number])
    return out


def apply_edits(text, per_line, manual, src):
    lines = text.split("\n")
    count = 0
    for number, change in per_line.items():
        line = lines[number - 1]
        for start, end, new in sorted(set(change["spans"]), reverse=True):
            line = line[:start] + new + line[end:]
            count += 1
        for item, token in change["imports"]:
            done = rewrite_import(line, item)
            if done is None:
                manual.append((src, number, token, "the import statement has a form the script does not rewrite"))
            else:
                count += done != line
                line = done
        lines[number - 1] = line
    return "\n".join(lines), count


def move_tree(root, old, new, done):
    """Rename, merging into a folder that exists. `done` records each real rename, so undo reverses exactly what happened."""
    source, target = root / old, root / new
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and source.is_dir():
        for child in sorted(source.iterdir()):
            move_tree(root, f"{old}/{child.name}", f"{new}/{child.name}", done)
        source.rmdir()
    else:
        os.rename(source, target)
        done.append((old, new))


def prune(root, rel):
    folder = (root / rel).parent
    while folder != root and folder.is_dir() and not any(folder.iterdir()):
        folder.rmdir()
        folder = folder.parent


def reverse_moves(root, done):
    for old, new in reversed(done):
        (root / old).parent.mkdir(parents=True, exist_ok=True)
        os.rename(root / new, root / old)
        prune(root, new)


def command_apply(args):
    root = Path(args.root).resolve()
    moves, also = load_plan(args.moves)
    errors = validate(root, moves)
    if errors:
        print("\n".join(f"error\t{e}" for e in errors))
        sys.exit(1)
    scan = Scan(root, also)
    edits, manual, fragile = compute(scan, moves)
    mapped = mapper(moves)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    state = root / STATE / stamp
    (state / "orig").mkdir(parents=True)
    done, edited, auto = [], [], 0
    planned = {}
    for index, (src, per_line) in enumerate(sorted(edits.items())):
        external = src.startswith("also:")
        path = Path(src[5:]) if external else root / src
        text = read_text(path)
        new_text, count = apply_edits(text, per_line, manual, src)
        if new_text != text:
            backup = f"{index:05d}"
            (state / "orig" / backup).write_bytes(text.encode("utf-8"))
            planned[src] = new_text
            edited.append({"src": src, "after": src if external else mapped(src), "orig": backup})
            auto += count
    try:
        for old in sorted(moves, key=len, reverse=True):
            move_tree(root, old, moves[old], done)
            prune(root, old)
        for item in edited:
            target = Path(item["src"][5:]) if item["src"].startswith("also:") else root / item["after"]
            target.write_bytes(planned[item["src"]].encode("utf-8"))
    except OSError as error:
        reverse_moves(root, done)
        for item in edited:
            target = Path(item["src"][5:]) if item["src"].startswith("also:") else root / item["src"]
            shutil.copyfile(state / "orig" / item["orig"], target)
        print(f"error\t{error}; everything was put back")
        sys.exit(1)
    after = Scan(root, also)
    journal = {"moves": moves, "done": done, "edited": edited, "also": also, "manual": manual, "fragile": fragile,
               "hashes": after.hashes, "logic": after.logic,
               "before": [e for e in scan.edges if e["how"] in HARD and not e["src"].startswith("also:")],
               "files": sorted(f for f, (k, _) in scan.files.items() if k == "code")}
    (state / "journal.json").write_text(json.dumps(journal, ensure_ascii=False), encoding="utf-8")
    print(f"STAMP\t{stamp}\nMOVED\t{len(done)}\nREWRITTEN\t{auto} references in {len(edited)} files\nMANUAL\t{len(manual)}\nFRAGILE\t{len(fragile)}")
    for row in manual[:args.limit]:
        print("manual\t" + f"{row[0]}:{row[1]}\t{row[2]}\t{row[3]}")
    for row in fragile[:args.limit]:
        print(f"fragile\t{row[0]}:{row[2]}\t{row[1]}")
    print(f"Next: fix the manual and fragile lines, then run: verify {args.root} {stamp}")


def command_plan(args):
    root = Path(args.root).resolve()
    moves, also = load_plan(args.moves)
    errors = validate(root, moves)
    for e in errors:
        print(f"error\t{e}")
    if errors:
        sys.exit(1)
    scan = Scan(root, also)
    edits, manual, fragile = compute(scan, moves)
    mapped = mapper(moves)
    moved_code = [f for f, (k, _) in scan.files.items() if k == "code" and mapped(f) != f]
    unimportable = sorted({mapped(f) for f in moved_code if f.endswith(".py") and dotted(mapped(f)) is None})
    auto = changed = 0
    for src, per_line in edits.items():
        text = read_text(Path(src[5:]) if src.startswith("also:") else root / src)
        new_text, count = apply_edits(text, per_line, manual, src)
        auto += count
        changed += new_text != text
    print(f"MOVES\t{len(moves)}\nCODE MOVED\t{len(moved_code)}\nAUTO\t{auto} references in {changed} files\nMANUAL\t{len(manual)}\nFRAGILE\t{len(fragile)}\nUNIMPORTABLE\t{len(unimportable)}")
    for row in manual[:args.limit]:
        print("manual\t" + f"{row[0]}:{row[1]}\t{row[2]}\t{row[3]}")
    for row in fragile[:args.limit]:
        print(f"fragile\t{row[0]}:{row[2]}\t{row[1]}")
    for rel in unimportable[:args.limit]:
        print(f"unimportable\t{rel}")


def syntax_errors(root, scan, targets):
    out = []
    node = shutil.which("node")
    for rel in sorted(targets):
        suffix = Path(rel).suffix.lower()
        path = root / rel
        if suffix == ".py":
            try:
                compile(read_text(path), rel, "exec")
            except (SyntaxError, ValueError, UnicodeDecodeError) as error:
                out.append((rel, type(error).__name__))
        elif suffix in {".js", ".cjs", ".mjs"} and node:
            run = subprocess.run([node, "--check", str(path)], capture_output=True)
            if run.returncode:
                out.append((rel, "node --check failed"))
    return out


def command_verify(args):
    root = Path(args.root).resolve()
    state = root / STATE / args.stamp
    journal = json.loads((state / "journal.json").read_text(encoding="utf-8"))
    mapped = mapper(journal["moves"])
    scan = Scan(root, journal["also"])
    found = collections.defaultdict(list)
    have = {(e["src"], e["dst"]) for e in scan.edges}
    for e in journal["before"]:
        src, dst = mapped(e["src"]), mapped(e["dst"])
        if src != dst and (src, dst) not in have and (src in scan.files or src in scan.dirs):
            found["lost"].append(f"{src}:{e['line']}\t{e['token']}\tused to reach {dst}")
    expected = {mapped(f) for f in journal["files"]}
    found["missing"] = [f for f in sorted(expected) if f not in scan.files and not (root / f).exists()]
    code = {f for f, (k, _) in scan.files.items() if k == "code"}
    changed = {f for f in code if journal["logic"].get(f) != scan.logic.get(f) and f in journal["logic"]}
    new = {f for f in code if f not in expected and Path(f).name != "__init__.py"}
    found["logic-changed"] = sorted(changed)
    found["new-code"] = sorted(new)
    tested = {e["dst"] for e in scan.edges if is_test(e["src"]) and e["how"] in {"import", "import~", "js", "path"}}
    stems = {Path(f).stem.removeprefix("test_").removesuffix("_test").split(".")[0] for f in code if is_test(f)}
    found["no-test"] = sorted(f for f in (changed | new) if not is_test(f) and f not in tested and Path(f).stem not in stems)
    touched = {mapped(f) for f in journal["files"] if mapped(f) != f} | {i["after"] for i in journal["edited"] if not i["src"].startswith("also:")} | changed | new
    found["syntax"] = [f"{a}\t{b}" for a, b in syntax_errors(root, scan, {f for f in touched if f in scan.files})]
    keys = [k for k in journal["moves"] if not (root / k).exists()]
    paths = [k for k in keys if "/" in k or "." in k]
    names = [k for k in keys if k not in paths]  # a one-word folder counts only as ROOT / "scripts" or as a path prefix, as in scripts/<name>.py
    pieces = []
    if paths:
        pieces.append(r"(?:(?<![\w.\-/])|(?<=\}/))(?:" + "|".join(map(re.escape, sorted(paths, key=len, reverse=True))) + r")(?![\w\-])")
    if names:
        joined = "|".join(map(re.escape, names))
        pieces.append(rf"""(?<=["'])(?:{joined})(?=["'])|(?:(?<![\w.\-/])|(?<=\}}/))(?:{joined})(?=/)""")
    if pieces:
        pattern = re.compile("|".join(pieces))
        for rel, (kind, size) in scan.files.items():
            if Path(rel).suffix.lower() in TEXT and size <= MAX_READ and not rel.startswith(("legacy/", "docs/core/")):
                try:
                    text = read_text(root / rel)
                except (UnicodeDecodeError, OSError):
                    continue
                for number, line in enumerate(text.split("\n"), 1):
                    if len(line) <= MAX_LINE:
                        for hit in pattern.findall(line):
                            if not (root / hit).exists() and not (root / posixpath.dirname(rel) / hit).exists():
                                found["old-mention"].append(f"{rel}:{number}\t{hit}")
    blocking = ("lost", "missing", "syntax", "no-test")
    for kind in blocking + ("logic-changed", "new-code", "old-mention"):
        rows = found[kind]
        print(f"{kind.upper()}\t{len(rows)}")
        for row in rows[:args.limit]:
            print(f"{kind}\t{row}")
    failed = sum(len(found[k]) for k in blocking)
    print(f"FAILED\t{failed}" if failed else "VERIFIED")
    sys.exit(1 if failed else 0)


def command_undo(args):
    root = Path(args.root).resolve()
    state = root / STATE / args.stamp
    journal = json.loads((state / "journal.json").read_text(encoding="utf-8"))
    later = []
    for rel, value in journal["hashes"].items():
        path = root / rel
        if not path.is_file() or sha(path.read_bytes()) != value:
            later.append(rel)
    if later and not args.force:
        print(f"CHANGED SINCE APPLY\t{len(later)}")
        for rel in later[:args.limit]:
            print(f"changed\t{rel}")
        print("Undo reverses the moves and restores rewritten files, so later edits to those files are lost. Pass --force to go ahead.")
        sys.exit(1)
    reverse_moves(root, [tuple(pair) for pair in journal["done"]])
    for item in journal["edited"]:
        target = Path(item["src"][5:]) if item["src"].startswith("also:") else root / item["src"]
        shutil.copyfile(state / "orig" / item["orig"], target)
    print(f"RESTORED\t{len(journal['done'])} moves, {len(journal['edited'])} files")


def command_map(args):
    scan = Scan(args.root, args.also)
    out = Path(args.out) if args.out else Path(args.root) / STATE / "map.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {"files": scan.files, "dirs": sorted(scan.dirs), "bulk": scan.bulk, "edges": scan.edges, "fragile": scan.fragile, "undecodable": scan.undecodable}
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"FILES\t{len(scan.files)}\nREFERENCES\t{len(scan.edges)}\nMAP\t{out.as_posix()}\n")
    summary(scan)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("map", help="every file and reference, with smells, clusters, hubs and folder cycles")
    p.add_argument("root")
    p.add_argument("--out")
    p.add_argument("--also", action="append", default=[])
    p = sub.add_parser("plan", help="check a move plan and list what it would change; writes nothing")
    p.add_argument("root")
    p.add_argument("moves")
    p.add_argument("--limit", type=int, default=60)
    p = sub.add_parser("apply", help="move files and rewrite references, keeping what undo needs")
    p.add_argument("root")
    p.add_argument("moves")
    p.add_argument("--limit", type=int, default=60)
    p = sub.add_parser("verify", help="after apply: lost references, syntax, changed code without a test, old mentions")
    p.add_argument("root")
    p.add_argument("stamp")
    p.add_argument("--limit", type=int, default=60)
    p = sub.add_parser("undo", help="reverse an apply")
    p.add_argument("root")
    p.add_argument("stamp")
    p.add_argument("--force", action="store_true")
    p.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    {"map": command_map, "plan": command_plan, "apply": command_apply, "verify": command_verify, "undo": command_undo}[args.command](args)


if __name__ == "__main__":
    main()
