import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
KST = timezone(timedelta(hours=9))
HOME = Path(tempfile.mkdtemp())
ENV = {**os.environ, "HOME": str(HOME), "USERPROFILE": str(HOME)}
ENV.pop("PHRO_DAILY_CHILD", None)
GOOD = "어제 완료:\n* 홍수 모델 파이프라인 구축\n\n오늘 할 일:\n* 홍수 모델 성능 개선\n"


def copy_of(project):
    plugin = Path(tempfile.mkdtemp())
    (plugin / "scripts").mkdir()
    (plugin / "skills/daily").mkdir(parents=True)
    shutil.copy(HERE / "scripts/daily.py", plugin / "scripts/daily.py")
    shutil.copy(HERE / "skills/daily/SKILL.md", plugin / "skills/daily/SKILL.md")
    (plugin / "origin.json").write_text(json.dumps({"project": str(project), "daily": False}), encoding="utf-8")
    return plugin


def load(plugin):
    spec = importlib.util.spec_from_file_location(f"daily_{plugin.name}", plugin / "scripts/daily.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def stub(text):
    folder = Path(tempfile.mkdtemp())
    (folder / "out.txt").write_text(text, encoding="utf-8")
    (folder / "stub.py").write_text("import sys, pathlib\nh = pathlib.Path(__file__).parent\n(h / 'prompt.txt').write_text(sys.stdin.read(), encoding='utf-8')\n"
                                    "sys.stdout.write((h / 'out.txt').read_text(encoding='utf-8'))\n", encoding="utf-8")
    return folder, json.dumps([sys.executable, "-I", "-X", "utf8", str(folder / "stub.py")])


def daily(plugin, *args, claude=None, extra=None):
    env = {**ENV, **({"PHRO_DAILY_CLAUDE": claude} if claude else {}), **(extra or {})}
    return subprocess.run([sys.executable, "-I", "-X", "utf8", str(plugin / "scripts/daily.py"), *args], capture_output=True, text=True,
                          encoding="utf-8", env=env, input="{}")


def line(kind, when, text, **extra):
    content = [{"type": "text", "text": text}]
    return json.dumps({"type": kind, "timestamp": when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                       "message": {"role": kind, "content": content}, **extra}, ensure_ascii=False)


def transcript(project, sid, rows, title=None):
    folder = HOME / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(project.resolve()))
    folder.mkdir(parents=True, exist_ok=True)
    text = "\n".join([*rows, *([json.dumps({"type": "custom-title", "customTitle": title}, ensure_ascii=False)] if title else [])]) + "\n"
    (folder / f"{sid}.jsonl").write_text(text, encoding="utf-8")
    return folder / f"{sid}.jsonl"


def test_period_and_due():
    d = load(copy_of(Path(tempfile.mkdtemp())))
    tue, mon = date(2026, 11, 10), date(2026, 11, 9)
    assert d.period(tue) == (datetime(2026, 11, 9, tzinfo=KST), datetime(2026, 11, 10, tzinfo=KST))
    assert d.period(mon)[0] == datetime(2026, 11, 6, tzinfo=KST), "Monday covers Friday to Sunday"
    assert d.name_of(tue) == "2026-11-10(화).md" and d.name_of(mon) == "2026-11-09(월).md"
    assert d.due(datetime(2026, 11, 10, 7, 59, tzinfo=KST)) is None
    assert d.due(datetime(2026, 11, 10, 8, 0, tzinfo=KST)) == tue
    assert d.due(datetime(2026, 11, 9, 23, 30, tzinfo=timezone.utc)) == tue, "UTC Monday 23:30 is Tuesday 08:30 KST"
    assert d.due(datetime(2026, 11, 14, 10, 0, tzinfo=KST)) is None, "no report on Saturday"


def test_tail_read():
    project = Path(tempfile.mkdtemp())
    d = load(copy_of(project))
    start, end = d.period(date(2026, 11, 10))
    big = "x" * 5000
    rows = [line("user", start - timedelta(hours=2), "before " + big), line("user", start + timedelta(hours=1), "in one"),
            json.dumps({"type": "file-history-snapshot"}), line("assistant", start + timedelta(hours=3), "in two " + big),
            line("user", end + timedelta(hours=1), "after")]
    path = transcript(project, "s1", rows)
    got = [json.loads(r)["message"]["content"][0]["text"][:6] for r in d.entries(path, start, end, chunk=1000)]
    assert got == ["in one", "in two"], got
    only = transcript(project, "s2", [line("user", start + timedelta(hours=1), "only")])
    assert len(d.entries(only, start, end, chunk=7)) == 1, "a file shorter than the period reads to its first line"


def test_session_text():
    project = Path(tempfile.mkdtemp())
    d = load(copy_of(project))
    start, end = d.period(date(2026, 11, 10))
    t = start + timedelta(hours=2)
    rows = [line("user", t, "<system-reminder>noise</system-reminder>홍수 모델 돌려"), line("assistant", t, "subagent work", isSidechain=True),
            line("user", t, "<local-command-caveat>x</local-command-caveat>"), line("assistant", t, "홍수 파이프라인을 만들었습니다.")]
    s = d.session(transcript(project, "s1", rows, title="홍수 작업"), start, end)
    assert s.splitlines() == ["## 홍수 작업", "- asked: 홍수 모델 돌려", "- replied: 홍수 파이프라인을 만들었습니다."], s
    quiet = transcript(project, "s2", [line("user", start - timedelta(days=2), "old")])
    assert d.session(quiet, start, end) is None


def test_private_sessions():
    project = Path(tempfile.mkdtemp())
    d = load(copy_of(project))
    start, end = d.period(date(2026, 11, 10))
    t = start + timedelta(hours=2)
    whole = transcript(project, "aaaa1111", [line("user", t, "비공개 설계 작업")], title="비공개 설계")
    part = transcript(project, "bbbb2222", [line("user", t, "공개 작업"), line("user", t + timedelta(hours=3), "비공개 부분")], title="비공개 섞인 제목")
    open_ = transcript(project, "cccc3333", [line("user", t, "보통 작업")], title="보통")
    d.PRIVATE = Path(tempfile.mkdtemp()) / "private-sessions.txt"
    a, b = (t + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"), (t + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M")
    d.PRIVATE.write_text(f"# private work\naaaa1111\nbbbb2222 {a} {b}  # a stretch, KST\nbroken1 x y\n", encoding="utf-8")
    hidden = d.private()
    assert hidden["aaaa1111"] is None and len(hidden["bbbb2222"]) == 1 and hidden["broken1"] is None, hidden
    assert d.session(whole, start, end, hidden.get("aaaa1111", ())) is None, "a listed session never reaches the report"
    s = d.session(part, start, end, hidden["bbbb2222"])
    assert "공개 작업" in s and "비공개 부분" not in s and "비공개 섞인 제목" not in s, s
    assert d.session(open_, start, end, hidden.get("cccc3333", ())).startswith("## 보통")
    real = HOME / ".claude/private-sessions.txt"
    shutil.copy(d.PRIVATE, real)
    try:
        text = daily(Path(d.__file__).parents[1], "digest", "--date", "2026-11-10").stdout
    finally:
        real.unlink()
    assert "비공개" not in text and "보통 작업" in text and "공개 작업" in text, text


def test_queue_sources():
    project = Path(tempfile.mkdtemp())
    d = load(copy_of(project))
    start, end = d.period(date(2026, 11, 10))
    t = start + timedelta(hours=2)
    u = lambda x: x.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (project / "queue").mkdir()
    rows = [(u(t), "pppp0000", "kept the private backbone"), (u(t), "nnnn1111", "kept the open backbone"),
            (u(t + timedelta(hours=3)), "ssss2222", "a stretch decision"), (u(t + timedelta(hours=1)), "ssss2222", "before the stretch"),
            (u(end + timedelta(hours=1)), "nnnn1111", "after the period")]
    (project / "queue/trail.tsv").write_text("ts\tsession\tphase\tdecision\twhy\tevidence\tresult\n"
                                             + "".join(f"{ts}\t{sid}\tsweep\t{dec}\tw\te\tkept\n" for ts, sid, dec in rows)
                                             + "broken row\n", encoding="utf-8")
    gen = int(end.timestamp())
    (project / "queue/board.json").write_text(json.dumps({"generated": gen, "tickets": [
        {"title": "private ticket", "status": "done", "session": "pppp0000", "changed_s": gen - int(t.timestamp())},
        {"title": "open ticket", "status": "done", "session": "nnnn1111", "changed_s": gen - int(t.timestamp())}]}), encoding="utf-8")
    d.PRIVATE = Path(tempfile.mkdtemp()) / "private-sessions.txt"
    a, b = (t + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"), (t + timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M")
    d.PRIVATE.write_text(f"pppp0000\nssss2222 {a} {b}\n", encoding="utf-8")
    hidden = d.private()
    assert d.decisions(project, start, end, hidden) == ["kept the open backbone (kept)", "before the stretch (kept)"], d.decisions(project, start, end, hidden)
    assert d.tickets(project, start, end, hidden)[0] == ["open ticket"], "a private session's ticket title stays out"
    real = HOME / ".claude/private-sessions.txt"
    shutil.copy(d.PRIVATE, real)
    try:
        text = daily(Path(d.__file__).parents[1], "digest", "--date", "2026-11-10").stdout
    finally:
        real.unlink()
    assert "kept the open backbone" in text and "open ticket" in text, text
    assert "private" not in text and "stretch decision" not in text and "after the period" not in text, text


def test_private_command():
    plugin = copy_of(Path(tempfile.mkdtemp()))
    real = HOME / ".claude/private-sessions.txt"
    me = {"CLAUDE_CODE_SESSION_ID": "dddd4444-0000"}
    try:
        assert daily(plugin, "private", extra=me).stdout == "PRIVATE\tdddd4444-0000\n", "the running session, whole"
        assert daily(plugin, "private", extra=me).stdout.startswith("EXISTS\t"), "never twice"
        out = daily(plugin, "private", "--session", "eeee5555", "--from", "2026-11-10T09:00", "--to", "2026-11-10T10:30", extra=me).stdout
        assert out == "PRIVATE\teeee5555 2026-11-10T00:00:00Z 2026-11-10T01:30:00Z\n", out
        assert "9999-01-01" in daily(plugin, "private", "--session", "ffff6666", "--from", "now").stdout, "from now on, open"
        assert daily(plugin, "private", "--from", "now", "--to", "2020-01-01", extra=me).stdout.startswith("FAILED"), "an end before the start"
        assert daily(plugin, "private", extra={"CLAUDE_CODE_SESSION_ID": ""}).stdout.startswith("FAILED"), "no session, no line"
        d = load(plugin)
        d.PRIVATE = real
        hidden = d.private()
        assert hidden["dddd4444-0000"] is None and hidden["eeee5555"][0][0] == datetime(2026, 11, 10, 0, 0, tzinfo=timezone.utc), hidden
        assert real.read_text(encoding="utf-8").startswith("#"), "a new file explains itself"
    finally:
        real.unlink(missing_ok=True)


def test_check():
    d = load(copy_of(Path(tempfile.mkdtemp())))
    assert d.check("```\n" + GOOD + "```") == GOOD
    many = "어제 완료:\n" + "".join(f"* 일 {i}\n" for i in range(6)) + "\n오늘 할 일:\n* x\n"
    for bad in ("오늘은 홍수 모델을 개선할 예정입니다.", "어제 완료:\n* 홍수 모델을 만들었습니다", "어제 완료:\n- 홍수 모델", "# 일일\n어제 완료:\n* x",
                "어제 완료 - 재난:\n* x\n\n오늘 할 일:\n* y",
                "어제 완료:\n* x\n\n어제 완료:\n* y\n\n오늘 할 일:\n* z",
                "어제 완료:\n* x",
                many,
                "어제 완료:\n* 졸음운전 판정 실험 설계 (089 데이터)\n\n오늘 할 일:\n* y",
                "어제 완료:\n* 화재 열원+탄 자국 실험\n\n오늘 할 일:\n* y",
                "어제 완료:\n* 재난 분석 결과 화면 개선 (프런트 전달)\n\n오늘 할 일:\n* y",
                "어제 완료:\n* 화재 복합 판독 실험 - 열원+연기(high) 단계 불통과로 종결, 열원+탄 자국(confirmed) 후보는 2024 봄 오탐 기준 통과\n\n오늘 할 일:\n* y"):
        try:
            d.check(bad)
        except ValueError:
            continue
        raise AssertionError(bad)


def test_run_writes_the_report():
    project = Path(tempfile.mkdtemp())
    (project / "docs/core").mkdir(parents=True)
    (project / "docs/core/README.md").write_text("# x\n\n## 지금 목표\n\n- 홍수 성능\n\n## 다른 절\n\n- y\n", encoding="utf-8")
    plugin = copy_of(project)
    start = datetime(2026, 11, 9, 10, tzinfo=KST)
    transcript(project, "s1", [line("user", start, "홍수 모델 돌려"), line("assistant", start, "돌렸습니다.")], title="홍수 작업")
    folder, claude = stub(GOOD)
    out = daily(plugin, "run", "--date", "2026-11-10", claude=claude)
    report = plugin / "docs/daily/2026-11-10(화).md"
    assert out.returncode == 0 and out.stdout.startswith("OK\tdaily\t"), out.stdout + out.stderr
    assert report.read_text(encoding="utf-8") == GOOD
    prompt = (folder / "prompt.txt").read_text(encoding="utf-8")
    assert "## 홍수 작업" in prompt and "홍수 모델 돌려" in prompt and "- 홍수 성능" in prompt and "다른 절" not in prompt
    assert "항목은 다섯 개까지" in prompt, "the style comes from the skill file"
    assert not (plugin / "docs/daily/.state/2026-11-10.lock").exists()
    assert daily(plugin, "run", "--date", "2026-11-10", claude=claude).stdout.startswith("EXISTS\t"), "never overwritten without --force"


def test_run_refuses_bad_form():
    plugin = copy_of(Path(tempfile.mkdtemp()))
    _, claude = stub("오늘은 홍수 모델을 개선할 예정입니다.")
    out = daily(plugin, "run", "--date", "2026-11-10", claude=claude)
    assert out.returncode == 1 and out.stdout.startswith("FAILED\t"), out.stdout
    assert not (plugin / "docs/daily/2026-11-10(화).md").exists()
    assert "not a heading" in (plugin / "docs/daily/.state/2026-11-10.error").read_text(encoding="utf-8") or \
        "first line" in (plugin / "docs/daily/.state/2026-11-10.error").read_text(encoding="utf-8")


def test_claim():
    plugin = copy_of(Path(tempfile.mkdtemp()))
    d = load(plugin)
    day = date(2026, 11, 10)
    assert d.claim(day) and not d.claim(day), "one session starts the run"
    (plugin / "docs/daily/.state/2026-11-10.lock").unlink()
    (plugin / "docs/daily/.state/2026-11-10.error").write_text("x", encoding="utf-8")
    assert not d.claim(day), "a fresh error waits 30 minutes"
    old = datetime.now().timestamp() - 3600
    os.utime(plugin / "docs/daily/.state/2026-11-10.error", (old, old))
    assert d.claim(day), "an old error is retried"
    (plugin / "docs/daily/.state/2026-11-10.lock").unlink()
    (plugin / "docs/daily/2026-11-10(화).md").write_text(GOOD, encoding="utf-8")
    assert not d.claim(day), "the report exists"


def test_silent_where_it_must_be():
    original = Path(tempfile.mkdtemp())
    (original / "scripts").mkdir()
    shutil.copy(HERE / "scripts/daily.py", original / "scripts/daily.py")
    for plugin, extra in ((original, None), (copy_of(Path(tempfile.mkdtemp())), {"PHRO_DAILY_CHILD": "1"})):
        out = daily(plugin, "hook", extra=extra)
        assert out.returncode == 0 and out.stdout == "" and out.stderr == "", out.stderr
        assert not (plugin / "docs/daily").exists(), "no state, no run"
    assert daily(original, "run").stdout.startswith("SKIPPED\t"), "the original plugin writes no report"
    assert daily(original, "on").stdout.startswith("SKIPPED\t"), "the original cannot be switched on"
    hooks = json.loads((HERE / "hooks/hooks.json").read_text(encoding="utf-8"))["hooks"]
    for event in ("SessionStart", "UserPromptSubmit"):
        assert any("daily.py" in h["command"] for g in hooks[event] for h in g["hooks"]), event


def test_off_until_switched_on():
    project = Path(tempfile.mkdtemp())
    plugin = copy_of(project)
    d = load(plugin)
    assert not d.enabled(), "a new copy starts off"
    (plugin / "origin.json").write_text(json.dumps({"project": str(project)}), encoding="utf-8")
    assert load(plugin).enabled(), "a copy made before the switch keeps running"
    (plugin / "origin.json").write_text(json.dumps({"project": str(project), "daily": False}), encoding="utf-8")
    d.due = lambda now: date(2026, 11, 10)
    d.hook()
    assert not (plugin / "docs/daily").exists(), "off: the hook claims nothing"
    assert daily(plugin, "on").stdout == "DAILY\ton\n"
    data = json.loads((plugin / "origin.json").read_text(encoding="utf-8"))
    assert data == {"project": str(project), "daily": True}, data
    assert load(plugin).enabled()
    assert daily(plugin, "off").stdout == "DAILY\toff\n" and not load(plugin).enabled()


def main():
    for name, t in sorted(globals().items()):
        if name.startswith("test_"):
            t()
    print("pass")


if __name__ == "__main__":
    main()
