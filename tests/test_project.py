import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/project.py"
sys.path.insert(0, str(SCRIPT.parent))
import project

SHARED = "\n".join(f"Shared rule number {i} that was copied into more than one instruction file." for i in range(4))


def write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def skill(name, description, body):
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n"


def grader(name):
    return f"---\ntype: tool_used\ntool: Skill\ninput_match: 'x?{name}\"'\n---\n\nok\n"


def clean(root):
    write(root, ".claude-plugin/plugin.json", json.dumps({"name": "demo", "version": "0.1.0"}))
    write(root, "skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials on a schedule.", "Read `references/steps.md`, then run `python scripts/rotate.py`."))
    write(root, "skills/alpha/references/steps.md", "Step one.\n")
    write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha` when a key expired."))
    write(root, "scripts/rotate.py", "import helper\nprint(helper.VALUE)\n")
    write(root, "scripts/helper.py", "VALUE = 1\n")
    for name in ("alpha", "beta"):
        write(root, f"evals/{name}-case/prompt.md", "do the thing\n")
        write(root, f"evals/{name}-case/graders/target.md", grader(name))


def run(defect=None):
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        if defect:
            defect(root)
        return project.findings(root), root


def kinds(found):
    return {f[0] for f in found}


def demo():
    found, _ = run()
    assert not found, found

    def broken(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`. Read `references/gone.md`."))
    assert ("broken-ref", "skills/beta/SKILL.md", "references/gone.md") in run(broken)[0]

    def duplicate(root):
        body = "A long enough body. " * 20
        write(root, "skills/alpha/references/one.md", body)
        write(root, "skills/beta/references/two.md", body)
        write(root, "skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials on a schedule.", "Read `references/steps.md`, `references/one.md`, run `python scripts/rotate.py`."))
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`. Read `references/two.md`."))
    assert kinds(run(duplicate)[0]) == {"duplicate-file"}

    def copied(root):
        write(root, "skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials on a schedule.", "Read `references/steps.md`, then run `python scripts/rotate.py`.\n" + SHARED))
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`.\n" + SHARED))
    assert kinds(run(copied)[0]) == {"shared-lines"}

    def echo(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports. Use it when finance asks for the month's invoices.",
                                                  "Hand off to `demo:alpha`. Use it when finance asks for the month's invoices."))
    assert run(echo)[0] == [("echo-description", "skill:beta", "Use it when finance asks for the month's invoices.")], run(echo)[0]

    def short_echo(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`. Renders invoice PDFs."))
    assert not run(short_echo)[0], "a short phrase shared with the description is not an echo"

    import inspect
    emitted = set(re.findall(r'\(\("([a-z-]+)"', inspect.getsource(project.findings) + inspect.getsource(project.case_findings) + inspect.getsource(project.hook_findings)))
    emitted |= {"echo-description", "duplicate-file", "unknown-frontmatter", "never-called", "never-read"}
    assert emitted == set(project.FIX), emitted ^ set(project.FIX)
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        write(root, "scripts/dead.py", "x = 1\n")
        ran = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "check", str(root)], capture_output=True, text=True, encoding="utf-8")
        assert ran.stdout.splitlines()[0] == "orphan\tscripts/dead.py\tno file refers to it\tdelete it, or name it where it is used", ran.stdout

    assert [project.error_kind(e) for e in ("Reached maximum number of turns", "Request timed out", "API Error: 529 overloaded", "boom")] == [
        "turn_limit", "timeout", "api_error", "other"], "the catch-all comes last, or it takes every error"

    def orphan(root):
        write(root, "scripts/dead.py", "print('never called')\n")
    assert ("orphan", "scripts/dead.py", "no file refers to it") in run(orphan)[0]

    def untested(root):
        write(root, "skills/gamma/SKILL.md", skill("gamma", "Compresses satellite imagery tiles for archive storage.", "Do it."))
    assert kinds(run(untested)[0]) == {"no-eval"}

    def in_route_cases(root):
        untested(root)
        write(root, "tests/route_cases.json", json.dumps([{"id": "c1", "skill": "gamma", "prompt": "compress"}]))
    assert not run(in_route_cases)[0], "a skill with routing cases is tested"

    def only_a_negative(root):
        untested(root)
        write(root, "tests/route_cases.json", json.dumps([{"id": "n1", "skill": None, "prompt": "do not fire gamma"}]))
    assert kinds(run(only_a_negative)[0]) == {"no-eval"}, "a negative case that names a skill does not test it"

    def leaky(root):
        in_route_cases(root)
        write(root, "tests/route_cases.json", json.dumps([{"id": "c1", "skill": "gamma", "prompt": "compress, this is a test"},
                                                          {"id": "c2", "skill": "gamma", "prompt": "타일 압축해줘"}]))
        write(root, "evals/quality/gamma/one/prompt.md", "압축 품질 평가용 입력\n")
        write(root, "evals/quality/gamma/two/prompt.md", "Compress these tiles for the archive.\n")
    assert sorted(f for f in run(leaky)[0] if f[0] == "leaky-case") == [
        ("leaky-case", "evals/quality/gamma/one/prompt.md", "평가"),
        ("leaky-case", "tests/route_cases.json#c1", "this is a test")], run(leaky)[0]

    def user_only(root):
        write(root, "skills/gamma/SKILL.md", "---\nname: gamma\ndescription: Compresses satellite imagery tiles for archive storage.\ndisable-model-invocation: true\n---\n\nDo it.\n")
    assert not run(user_only)[0], "routing cannot select a skill only the user invokes"

    def overlap(root):
        write(root, "skills/gamma/SKILL.md", skill("gamma", "Renders invoice PDFs from monthly billing exports for accounting.", "Do it."))
        write(root, "evals/gamma-case/prompt.md", "x\n")
        write(root, "evals/gamma-case/graders/target.md", grader("gamma"))
    assert kinds(run(overlap)[0]) == {"overlap-candidate"}

    def long_description(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoices. " * 100, "Hand off to `demo:alpha`."))
    assert "long-description" in kinds(run(long_description)[0])

    def long_body(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`.\n" + ("A long line of instructions. " * 60 + "\n") * 40))
    assert kinds(run(long_body)[0]) == {"long-body"}, "a compaction keeps the first 5,000 tokens, however few the lines"

    def unpinned_mcp(root):
        servers = {"loose": {"command": "npx", "args": ["-y", "@acme/files-mcp"]},
                   "latest": {"command": "uvx", "args": ["notes-mcp@latest"]},
                   "pinned": {"command": "npx", "args": ["-y", "@acme/files-mcp@1.4.2"]},
                   "from": {"command": "uvx", "args": ["--from", "notes-mcp==0.3.1", "notes"]},
                   "local": {"command": "python", "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/server.py"]}}
        write(root, ".mcp.json", json.dumps({"mcpServers": servers}))
    assert sorted(f for f in run(unpinned_mcp)[0] if f[0] != "orphan") == [
        ("mcp-unpinned", "mcp:latest", "notes-mcp@latest"), ("mcp-unpinned", "mcp:loose", "@acme/files-mcp")], "a server fetched by name runs whatever is newest"

    def typo_key(root):
        write(root, "skills/beta/SKILL.md", "---\nname: beta\ndescription: Renders invoice PDFs from monthly billing exports.\n"
              "disable_model_invocation: true\nwhen_to_use: monthly close\nmetadata:\n  owner: billing\n---\n\nHand off to `demo:alpha`.\n")
    assert run(typo_key)[0] == [("unknown-frontmatter", "skill:beta", "disable_model_invocation")], "Claude Code ignores a key it does not know, silently"

    def hooks(root):
        events = {"SessionStart": [{"matcher": "startup", "hooks": [{"type": "command", "command": "python x.py", "timeout": 5}]}],
                  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python x.py"}]}],
                  "PostToolUse": [{"hooks": [{"type": "command", "command": "python x.py", "async": True}]}],
                  "SessionBegin": [{"hooks": [{"type": "command", "command": "python x.py", "timeout": 5}]}]}
        write(root, "hooks/hooks.json", json.dumps({"description": "demo", "hooks": events}))
    assert sorted(run(hooks)[0]) == [("hook-config", "hooks/hooks.json", "SessionBegin is not a hook event"),
                                     ("hook-config", "hooks/hooks.json", "UserPromptSubmit command has no timeout, so it can hold the session for the default")], run(hooks)[0]

    def many_short_lines(root):
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`.\n" + "Do it.\n" * 600))
    assert not run(many_short_lines)[0], "lines alone cost little"

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        empty = Path(d) / "no-transcripts"
        empty.mkdir()
        assert kinds(project.findings(root, empty)) == {"unmeasured"}, "no transcripts says nothing about use"
        seen = Path(d) / "transcripts"
        seen.mkdir()
        call = {"type": "assistant", "uuid": "1", "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": "demo:alpha"}}]}}
        (seen / "a.jsonl").write_text(json.dumps(call) + "\n", encoding="utf-8")
        assert [f[1] for f in project.findings(root, seen)] == ["skill:beta", "skills/alpha/references/steps.md"], "with transcripts, an unused skill and an unread reference still show"

    with tempfile.TemporaryDirectory() as d:
        proj = Path(d) / "proj"
        write(proj, "src/app.py", "print(1)\n")
        write(proj, ".claude/skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials on a schedule.", "Do it."))
        write(proj, ".claude/settings.local.json", '{"env": {"NAME": "value"}}')
        write(proj, ".claude/worktrees/w/skills/ghost/SKILL.md", skill("ghost", "A copy in a worktree.", "Do it."))
        write(proj, ".claude/tests/route_cases.json", json.dumps([{"id": "c1", "skill": "alpha", "prompt": "rotate"}]))
        assert project.plugin_root(proj) == proj.resolve() / ".claude", "a project's capabilities live in its .claude"
        assert project.plugin_root(proj / ".claude") == proj.resolve() / ".claude"

        def cli(*args):
            return subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), *args], capture_output=True, text=True, encoding="utf-8").stdout
        said = cli("summary", str(proj))
        assert "skill:alpha" in said and "ghost" not in said, said
        assert cli("check", str(proj)).strip() == "FINDINGS\t0", "a routing case in the project's .claude counts as tested"

        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps([
            {"id": "o1", "skill": "alpha", "prompt": "write the draft", "after": "Write", "rule": "After a draft, call alpha."},
            {"id": "c1", "skill": "alpha", "prompt": "rotate"},
        ]), encoding="utf-8")
        built = Path(d) / "built"
        said = cli("build", str(proj), str(cases), str(built), "--stub")
        assert (built / "skills/alpha/SKILL.md").exists() and not (built / "settings.local.json").exists() and not (built / "worktrees").exists(), said
        assert "--allow-tools Write Edit" in said, "a step needs its tool granted"
        order = (built / "evals/o1/graders/order.md").read_text(encoding="utf-8")
        assert "type: tool_order" in order and "before: Write" in order and "tool: Skill" in order and "alpha" in order, order
        first = (built / "evals/o1/prompt.md").read_text(encoding="utf-8")
        assert "max_turns: 10" in first and "Write" in first and 'append_system_prompt: "After a draft, call alpha."' in first, first
        plain = (built / "evals/c1/prompt.md").read_text(encoding="utf-8")
        assert "max_turns: 4" in plain and "Write" not in plain and "append_system_prompt" not in plain, plain
        assert not (built / "evals/c1/graders/order.md").exists()

    with tempfile.TemporaryDirectory() as d:
        extra = skill("gamma", "Compresses satellite imagery tiles for archive storage.", "Do it.")
        manifest_only = Path(d) / "manifest-only"
        write(manifest_only, ".claude-plugin/plugin.json", json.dumps({"name": "demo", "version": "0.1.0"}))
        write(manifest_only, ".claude/skills/gamma/SKILL.md", extra)
        assert project.plugin_root(manifest_only) == manifest_only.resolve(), "a plugin folder stays as given, whatever else it holds"
        skills_folder = Path(d) / "skills-folder"
        write(skills_folder, "skills/alpha/SKILL.md", extra)
        write(skills_folder, ".claude/skills/gamma/SKILL.md", extra)
        assert project.plugin_root(skills_folder) == skills_folder.resolve(), "so does a folder of skills without a manifest"
        plain = Path(d) / "plain"
        plain.mkdir()
        assert project.plugin_root(plain) == plain.resolve(), "and a folder with no skills anywhere"

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        db = project.scan(root)[0]
        up = {r[0] for r in db.execute("select src from edge where dst = 'scripts/helper.py'")}
        assert up == {"scripts/rotate.py"}, up
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "impact", str(root), "helper.py"], capture_output=True, text=True, encoding="utf-8").stdout
        assert "scripts/rotate.py" in out and "skill:alpha" in out and "skill:beta" in out and "eval:alpha-case" in out, out
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "impact", str(root), "scripts/rotate.py"], capture_output=True, text=True, encoding="utf-8").stdout
        assert "only it uses\tscripts/helper.py" in out, out

        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps([{"id": "c1", "skill": "alpha", "prompt": "rotate"}, {"id": "n1", "skill": None, "prompt": "hello"}]), encoding="utf-8")
        out_dir = Path(d) / "copy"
        args = [sys.executable, "-I", "-X", "utf8", str(SCRIPT), "build", str(root), str(cases), str(out_dir), "--stub"]
        subprocess.run(args, check=True, capture_output=True)
        (out_dir / "result.json").write_text("{}", encoding="utf-8")
        again = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
        assert again.returncode != 0 and "exists" in again.stderr and (out_dir / "result.json").exists(), "a rebuild must not wipe an earlier run's evidence"
        assert "Stop here" in (out_dir / "skills/alpha/SKILL.md").read_text(encoding="utf-8")
        assert "Read `references" in (root / "skills/alpha/SKILL.md").read_text(encoding="utf-8"), "the plugin itself must stay untouched"
        assert "max: 0" in (out_dir / "evals/n1/graders/target.md").read_text(encoding="utf-8")
        assert (out_dir / "evals/c1/graders/to-beta.md").exists()

        def result(name, target, took, error=None, negative=False):
            graders = [{"name": "target", "passed": target}] + [{"name": f"to-{n}", "passed": n == took} for n in ("alpha", "beta")]
            spec = [{"name": "target", "config": {"max": 0} if negative else {"input_match": '"skill"\\s*:\\s*"(?:[\\w-]+:)?alpha"'}}]
            return {"name": name, "graders": spec, "arms": {"with": [{"graders": graders, "error": error}]}}
        cut = "exit 1: Reached maximum number of turns (2)"
        api = "exit 1: API Error: 529 overloaded"
        late = "timed out after 300s"
        doc = {"costUsd": 1, "partial": False, "cases": [
            result("c1", True, "alpha"), result("c2", False, "beta", cut), result("c3", False, None, cut), result("c4", False, None),
            result("c5", False, None, api), result("c6", True, "alpha", late),
            result("n1", True, None, api, negative=True), result("n2", True, None, negative=True), result("n3", False, "alpha", late, negative=True),
        ]}
        path = Path(d) / "result.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "tally", str(path)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "target\t2/6\tfalse_fires\t1/2" in out and "alpha\t6\t2\t1\t2\t1" in out and "c4 alpha -> none" in out, out
        assert "c3 alpha -> unresolved turn_limit, " + cut in out and "c5 alpha -> unresolved api_error, " + api in out, "an abnormal end is not a miss, whatever ended it"
        assert "n1 no skill expected -> unresolved api_error, " + api in out and "false_fire\tn3 -> alpha" in out, "a negative that never ran proves nothing"
        assert "rerun\tturn_limit\t1\t" in out and "rerun\tapi_error\t2\t" in out, "each kind of abnormal end says what to do next, once"
        assert "rerun\ttimeout" not in out, "n3 fired before its timeout, so it is a result, not an unresolved run"

        accepted = Path(d) / "accepted.json"
        subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "tally", str(path), "--save", str(accepted)], check=True, capture_output=True)
        saved = json.loads(accepted.read_text(encoding="utf-8"))
        assert saved["c1"] == "hit" and saved["c2"] == "other:beta" and saved["c3"] == "unresolved" and saved["n3"] == "false_fire:alpha", saved
        doc["cases"][3] = result("c4", True, "alpha")
        doc["cases"][0] = result("c1", False, "beta")
        doc["cases"][2] = result("c3", True, "alpha")
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "tally", str(path), "--accepted", str(accepted)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "gained\tc4\tnone -> hit" in out and "broken\tc1\thit -> other:beta" in out, out
        assert "gained\tc3" not in out and "not compared\tc3" in out, "an unresolved run on either side proves no change"

        split = Path(d) / "split.json"
        split.write_text(json.dumps([{"id": "c1"}, {"id": "c2"}, {"id": "c3", "split": "test"}, {"id": "c4", "split": "test"},
                                     {"id": "c5", "split": "test"}, {"id": "n2", "split": "test"}, {"id": "n3"}]), encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "tally", str(path), "--cases", str(split)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "split\twrite\ttarget\t1/3\tfalse_fires\t1/1" in out, out
        assert "split\ttest\ttarget\t2/2\tfalse_fires\t0/1" in out, "cases held out from writing the description are scored apart, unresolved ones left out"
        plain = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "tally", str(path)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "split\t" not in plain, "no split lines without held-out cases"

        def ordered(name, in_order):
            case = result(name, True, "alpha")
            case["arms"]["with"][0]["graders"].append({"name": "order", "passed": in_order})
            return case
        doc = {"costUsd": 1, "partial": False, "cases": [ordered("o1", True), ordered("o2", False), result("c1", True, "alpha")]}
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "tally", str(path)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "target\t2/3" in out and "alpha\t3\t2\t0\t0\t0\t1" in out and "wrong_order" in out, out
        assert "o2 alpha -> called, but not after the step" in out, "a call at the wrong moment is not a hit"

        def arm(passes, n=3):
            return [{"costUsd": 0.1, "error": None, "graders": [{"name": g, "passed": i < k} for g, k in passes.items()]} for i in range(n)]
        defs = [{"name": "skill", "type": "tool_used", "config": {"tool": "Skill"}}] + [{"name": g, "type": "regex", "config": {}} for g in ("b-ban", "f-num", "j-faith")]
        doc = {"partial": False, "cases": [
            {"name": "q1", "graders": defs, "arms": {"with": arm({"skill": 3, "b-ban": 3, "f-num": 3, "j-faith": 2}), "without": arm({"b-ban": 1, "f-num": 3, "j-faith": 0})}},
            {"name": "q2", "graders": defs, "arms": {"with": arm({"skill": 2, "b-ban": 2, "f-num": 3, "j-faith": 3}), "without": arm({"b-ban": 0, "f-num": 0, "j-faith": 0})}},
            {"name": "q3", "graders": defs[1:2], "arms": {"with": arm({"b-ban": 3}), "without": arm({"b-ban": 3})}},
        ]}
        path.write_text(json.dumps(doc), encoding="utf-8")
        accepted = Path(d) / "accepted.json"
        accepted.write_text(json.dumps({"q1": {"b-ban": 1.0, "f-num": 1.0, "j-faith": 1 / 3}, "q2": {"b-ban": 1.0, "f-num": 1.0}}), encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "quality", str(path), "--accepted", str(accepted)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "case\tq1\tfired\t3/3" in out and "case\tq2\tfired\t2/3" in out, "the Skill indicator shows firing and is not scored"
        assert "case\tq3\tfired\t-" in out, "a case without an indicator shows unknown firing, not 0"
        assert "break\tq2\tb-ban\t0.67" in out and "q1\tb-ban\t1.00\t0.33\t+0.67\t\n" in out, "a boundary that fails one run is a break"
        assert "q1\tf-num\t1.00\t1.00\t+0.00\tnondiscriminating" in out and "q1\tj-faith\t0.67\t0.00\t+0.67\tflaky" in out, out
        assert "gained\tq1\tj-faith\t0.33 -> 0.67" not in out, "one run in three moving is noise"
        assert "new\tq2\tj-faith\t1.00" in out and "broken" not in out, out
        assert "\tskill\t" not in out.split("case\tgrader")[1], "the indicator has no row"

        broke = arm({"skill": 3, "b-ban": 3, "f-num": 3, "j-faith": 3})
        broke[2] = {"costUsd": 0.1, "error": "exit 1: API Error: 529 overloaded", "graders": [{"name": g["name"], "passed": False} for g in defs]}
        doc = {"partial": False, "cases": [{"name": "q4", "graders": defs, "arms": {"with": broke, "without": arm({"b-ban": 0, "f-num": 0, "j-faith": 0})}}]}
        path.write_text(json.dumps(doc), encoding="utf-8")
        out = subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), "quality", str(path)], capture_output=True, text=True, encoding="utf-8").stdout
        assert "case\tq4\tfired\t2/2\tunresolved\t1" in out, "an aborted run is neither a firing nor a miss"
        assert "break\t" not in out and "q4\tb-ban\t1.00\t0.00\t+1.00\t\n" in out, "a run that never finished does not fail its graders"
        assert "rerun\tapi_error\t1\t" in out, out

    def cli(*args):
        return subprocess.run([sys.executable, "-I", "-X", "utf8", str(SCRIPT), *map(str, args)], capture_output=True, text=True, encoding="utf-8").stdout

    tool = "mcp__plugin_demo_files__search"
    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        cases = Path(d) / "cases.json"
        cases.write_text(json.dumps([{"id": "t1", "tool": tool, "prompt": "find last month's invoice files and list them"},
                                     {"id": "t2", "tool": tool, "prompt": "after drafting, search", "after": "Write", "rule": "After a draft, search the files."}]), encoding="utf-8")
        built = Path(d) / "built"
        cli("build", root, cases, built, "--stub")
        target = (built / "evals/t1/graders/target.md").read_text(encoding="utf-8")
        assert f"tool: {tool}\n" in target and "input_match" not in target, target
        assert tool in (built / "evals/t1/prompt.md").read_text(encoding="utf-8"), "the run must be allowed to call the tool"
        assert f"after: {{ tool: {tool} }}" in (built / "evals/t2/graders/order.md").read_text(encoding="utf-8")

        def tool_result(name, passed, took=None):
            graders = [{"name": "target", "passed": passed}] + [{"name": f"to-{n}", "passed": n == took} for n in ("alpha", "beta")]
            return {"name": name, "graders": [{"name": "target", "config": {"tool": tool}}], "arms": {"with": [{"graders": graders, "error": None}]}}
        path = Path(d) / "result.json"
        path.write_text(json.dumps({"costUsd": 1, "partial": False, "cases": [tool_result("t1", True), tool_result("t3", False, "beta")]}), encoding="utf-8")
        out = cli("tally", path)
        assert f"{tool}\t2\t1\t1\t0\t0\t0" in out and f"t3 {tool} -> beta" in out, out

    with tempfile.TemporaryDirectory() as d:
        root = Path(d) / "demo"
        clean(root)
        write(root, "skills/beta/SKILL.md", skill("beta", "Renders invoice PDFs from monthly billing exports.", "Hand off to `demo:alpha`. Read `references/layout.md`."))
        write(root, "skills/beta/references/layout.md", "Layout.\n")
        write(root, "skills/beta/CONTRACT.md", "Output: an invoice PDF.\n")
        seen = Path(d) / "transcripts"
        events = [{"type": "assistant", "uuid": str(i), "message": {"content": [{"type": "tool_use", "name": "Skill", "input": {"skill": f"demo:{n}"}}]}}
                  for i, n in enumerate(("alpha", "beta"))]
        events.append({"type": "assistant", "uuid": "r", "isSidechain": True, "message": {"content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": str(root / "skills/alpha/references/steps.md").replace("/", "\\")}}]}})
        write(seen, "a.jsonl", "".join(json.dumps(e) + "\n" for e in events))
        assert project.findings(root, seen) == [("never-read", "skills/beta/references/layout.md", "0 Reads in these transcripts")], project.findings(root, seen)
        summary = cli("summary", root, "--usage", seen)
        assert "skills/alpha/references/steps.md\t1" in summary and "skills/beta/references/layout.md\t0" in summary, summary

    with tempfile.TemporaryDirectory() as d:
        copy = Path(d) / "copy"
        clean(copy)
        write(copy, "skills/alpha/CONTRACT.md", "Output: a rotation plan.\n")

        def doc(model):
            graders = [{"name": "target", "passed": True}, {"name": "to-alpha", "passed": True}]
            return {"costUsd": 1, "partial": False, "claudeVersion": "2.1.300", "suite": {"modelOverride": model, "root": str(copy)},
                    "cases": [{"name": "c1", "graders": [{"name": "target", "config": {"input_match": '"skill"\\s*:\\s*"(?:[\\w-]+:)?alpha"'}}],
                               "arms": {"with": [{"graders": graders, "error": None}], "without": [{"graders": [{"name": "f-plan", "passed": True}], "error": None}]}}]}
        path, accepted = Path(d) / "r.json", Path(d) / "accepted.json"
        path.write_text(json.dumps(doc("claude-opus-5-5")), encoding="utf-8")
        cli("tally", path, "--save", accepted)
        meta = json.loads(accepted.read_text(encoding="utf-8"))["_meta"]
        assert meta["model"] == "claude-opus-5-5" and meta["claude"] == "2.1.300" and set(meta["files"]) == {"skill:alpha", "skill:beta"}, meta
        out = cli("tally", path, "--accepted", accepted)
        assert "stale" not in out and "_meta" not in out, "an unchanged baseline is silent"
        write(copy, "skills/alpha/SKILL.md", skill("alpha", "Rotates database credentials and API keys on a schedule.", "Read `references/steps.md`."))
        path.write_text(json.dumps(doc("claude-sonnet-5-5")), encoding="utf-8")
        out = cli("tally", path, "--accepted", accepted)
        assert "stale\tmodel\tclaude-opus-5-5 -> claude-sonnet-5-5\toutcomes are not comparable" in out, out
        assert "stale\tchanged\tskill:alpha" in out and "skill:beta" not in out, "a routing baseline follows descriptions"

        qdoc = doc("claude-opus-5-5")
        qdoc["cases"][0]["arms"]["with"][0]["graders"].append({"name": "f-plan", "passed": True})
        path.write_text(json.dumps(qdoc), encoding="utf-8")
        cli("quality", path, "--save", accepted)
        assert "skills/alpha/CONTRACT.md" in json.loads(accepted.read_text(encoding="utf-8"))["_meta"]["files"]
        write(copy, "skills/alpha/CONTRACT.md", "Output: a rotation plan with dates.\n")
        out = cli("quality", path, "--accepted", accepted)
        assert "stale\tchanged\tskills/alpha/CONTRACT.md" in out and "new\t_meta" not in out, out
    print("ok")


if __name__ == "__main__":
    demo()
