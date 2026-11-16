"""Creating an optional venv (kind "pdf": pypdfium2; kind "mpp": MPXJ and JPype, with a Java found on PATH).
Runs only from `read.py setup`: nothing else imports it.

Each step that touches the system sits in `Steps`, so tests replace them with stubs.
"""
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import cache, pyenv

PIP_TIMEOUT = 900
STALE_SECONDS = 1080  # past PIP_TIMEOUT, so a slow but live pip never loses its lock or its tmp folder
READ_PY = Path(__file__).resolve().parents[1] / "read.py"
PIP_ARGS = ["-I", "-m", "pip", "install", "--isolated", "--no-input", "--disable-pip-version-check",
            "--only-binary", ":all:", "--no-deps", "--require-hashes", "-r"]


def _smoke_pdf() -> bytes:
    """A one-page Helvetica pdf of `phro smoke`, with true xref offsets."""
    content = b"BT /F1 12 Tf 72 720 Td (phro smoke) Tj ET"
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, start)
    return bytes(out)


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kw)


class Steps:
    """The real steps. Only `setup` run by the user calls them."""

    def make_venv(self, tmp):
        r = _run([sys.executable, "-I", "-m", "venv", str(tmp)], cwd=Path(tmp).parent)
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())

    def pip(self, python, requirements):
        try:
            r = _run([str(python), *PIP_ARGS, str(requirements)], cwd=Path(python).parent.parent.parent,
                     timeout=PIP_TIMEOUT)
        except subprocess.TimeoutExpired:
            return False, "timeout"
        return r.returncode == 0, (r.stdout + r.stderr).strip()

    def versions(self, python):
        code = ("import json; from pypdfium2 import version as v; "
                "print(json.dumps([str(v.PYPDFIUM_INFO.version), v.PDFIUM_INFO.build]))")
        r = _run([str(python), "-I", "-X", "utf8", "-c", code])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        version, build = json.loads(r.stdout.strip().splitlines()[-1])
        return version, build, pyenv.record_digests(Path(python).parent.parent)

    def smoke(self, python):
        work = Path(python).parent.parent
        pdf, out = work / "smoke.pdf", work / "smoke-out"
        try:
            pdf.write_bytes(_smoke_pdf())
            r = _run([str(python), "-I", "-X", "utf8", str(READ_PY), "convert", str(pdf), "--engine", "pdfium",
                      "--out", str(out)])
            md = out / "out.md"
            return r.returncode == 0 and md.is_file() and "phro smoke" in md.read_text(encoding="utf-8")
        except OSError:
            return False
        finally:
            pdf.unlink(missing_ok=True)
            shutil.rmtree(out, ignore_errors=True)


SMOKE_TIMEOUT = 180
# Run in the venv with -I -c: argv[1] is the recorded JVM library. Starting the JVM through JPype and loading
# MPPReader catches a 32/64-bit mismatch between Python and Java, and a Java that cannot host MPXJ.
SMOKE_MPP = (
    "import sys, jpype, mpxj\n"
    "jpype.startJVM(sys.argv[1], '-Xmx1024m', '-Djava.awt.headless=true')\n"
    "jpype.JClass('org.mpxj.mpp.MPPReader')\n"
    "print('phro-mpp-ok')\n")


class MppSteps(Steps):
    """Steps for kind "mpp": the java check, the versions of mpxj and jpype1, and the JPype smoke run."""

    def java(self):
        return pyenv.java_info()

    def versions(self, python):
        code = ("import json; from importlib.metadata import version as v; "
                "print(json.dumps([v('mpxj'), v('jpype1')]))")
        r = _run([str(python), "-I", "-X", "utf8", "-c", code])
        if r.returncode != 0:
            raise RuntimeError((r.stderr or r.stdout).strip())
        mpxj, jpype = json.loads(r.stdout.strip().splitlines()[-1])
        return mpxj, jpype, pyenv.record_digests(Path(python).parent.parent, "mpp")

    def smoke(self, python, java):
        try:
            r = _run([str(python), "-I", "-X", "utf8", "-c", SMOKE_MPP, java["jvm"]], timeout=SMOKE_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and "phro-mpp-ok" in r.stdout


def _flat(text, limit=300) -> str:
    return " ".join(str(text).split())[:limit]


def _sibling(venv, suffix) -> Path:
    return Path(str(venv) + suffix)


def _old(path, seconds=STALE_SECONDS) -> bool:
    try:
        return path.stat().st_mtime < time.time() - seconds
    except OSError:
        return False


def _take_lock(lock: Path):
    """Create the lock exclusively with a random token in it; the token, or None when another setup holds it."""
    token = secrets.token_hex(8)
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if _old(lock):
                lock.unlink(missing_ok=True)
                continue
            return None
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(token)
        return token
    return None


def _release(lock: Path, token: str) -> None:
    """Remove the lock only while it still holds our token (a stale-lock takeover keeps its own)."""
    try:
        if lock.read_text(encoding="utf-8") == token:
            lock.unlink()
    except OSError:
        pass


def _is_venv(path: Path) -> bool:
    return (path / "pyvenv.cfg").is_file()


def _replaceable(path: Path, kind="pdf") -> bool:
    """Setup replaces only nothing, an empty folder, or a venv of this kind (a ready.json naming another kind
    refuses; one with no kind is a 2d pdf venv)."""
    if not path.exists():
        return True
    if not path.is_dir():
        return False
    if not any(path.iterdir()):
        return True
    if not _is_venv(path):
        return False
    ready = pyenv._ready(path)
    return ready is None or pyenv.ready_kind(ready) == kind


def _not_venv(path, kind="pdf") -> str:
    return f"FAIL\tinstall\t{path} is not a {kind} venv"


def _python_info(tmp: Path) -> dict:
    version = home = None
    try:
        for line in (tmp / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().lower() == "home":
                home = v.strip()
            elif sep and k.strip().lower() in ("version", "version_info"):
                version = v.strip()
    except OSError:
        pass
    return {"version": version or sys.version.split()[0], "home": home}


def _clean_leftovers(venv: Path, tmp: Path) -> None:
    """Remove stale DIR.tmp-* and DIR.old-* folders, matched by name prefix (never a glob)."""
    for p in venv.parent.iterdir():
        mine = p.name.startswith(venv.name + ".tmp-") or p.name.startswith(venv.name + ".old-")
        if mine and p != tmp and _old(p):
            shutil.rmtree(p, ignore_errors=True)


def setup(venv, steps=None, kind="pdf") -> str:
    pyenv._check(kind)
    steps = steps or (MppSteps() if kind == "mpp" else Steps())
    java = None
    if kind == "mpp":  # before anything is made: no Java, no folder
        java = steps.java()
        if isinstance(java, str):
            return f"FAIL\tjava\t{_flat(java)}"
    venv = Path(venv).resolve()
    try:
        venv.parent.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"FAIL\tinstall\t{_flat(e)}"
    lock = _sibling(venv, ".lock")
    token = _take_lock(lock)
    if token is None:
        return "FAIL\tlock\tanother setup is running"
    tmp = _sibling(venv, f".tmp-{os.getpid()}")
    try:
        if not _replaceable(venv, kind):
            return _not_venv(venv, kind)
        _clean_leftovers(venv, tmp)
        shutil.rmtree(tmp, ignore_errors=True)
        try:
            return _build(venv, tmp, steps, kind, java)
        except Exception as e:  # noqa: BLE001
            return f"FAIL\tinstall\t{_flat(e)}"
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    finally:
        _release(lock, token)


def _build(venv: Path, tmp: Path, steps, kind="pdf", java=None) -> str:
    try:
        steps.make_venv(tmp)
    except Exception as e:  # noqa: BLE001
        return f"FAIL\tvenv\t{_flat(e)}"
    python = pyenv.interpreter(tmp)
    try:
        ok, output = steps.pip(python, pyenv.REQUIREMENTS[kind])
    except Exception as e:  # noqa: BLE001
        return f"FAIL\tpip\t{_flat(e)}"
    if not ok:
        low = output.lower()
        if "no matching distribution" in low or "could not find a version" in low:
            return "FAIL\tpip\tno wheel for this platform"
        return f"FAIL\tpip\t{_flat(output)}"
    try:
        version, build, records = steps.versions(python)
    except Exception as e:  # noqa: BLE001
        return f"FAIL\tversions\t{_flat(e)}"
    try:
        smoked = steps.smoke(python, java) if kind == "mpp" else steps.smoke(python)
    except Exception as e:  # noqa: BLE001
        return f"FAIL\tsmoke\t{_flat(e)}"
    if not smoked:
        return ("FAIL\tsmoke\tJPype could not start the JVM and load MPXJ" if kind == "mpp"
                else "FAIL\tsmoke\tthe test page did not read with pdfium")
    ready = {"kind": kind, "requirements": pyenv.requirements_hash(kind), "python": _python_info(tmp)}
    if kind == "mpp":  # versions() gave (mpxj, jpype1, records)
        ready.update({"mpxj": version, "jpype1": build, "java": java, "records": records})
        engine = pyenv.mpp_engine_id(version, java["major"])
    else:
        ready.update({"pypdfium2": version, "pdfium": build, "records": records})
        engine = pyenv.engine_id(version, build)
    (tmp / "ready.json").write_text(json.dumps(ready), encoding="utf-8")
    if not _replaceable(venv, kind):  # looked at again right before the swap
        return _not_venv(venv, kind)
    old = _sibling(venv, f".old-{os.getpid()}")
    shutil.rmtree(old, ignore_errors=True)
    moved = False
    try:
        if venv.exists():
            os.replace(venv, old)
            moved = True
        os.replace(tmp, venv)
    except OSError as e:
        if moved and not venv.exists():
            try:
                os.replace(old, venv)
            except OSError as e2:
                return f"FAIL\tinstall\tcould not restore {old} to {venv}: {_flat(e2)} (after {_flat(e)})"
        return f"FAIL\tinstall\t{_flat(e)}"
    shutil.rmtree(old, ignore_errors=True)
    try:
        root = cache.root()
        (root / f"{kind}-venv.json").write_text(json.dumps({"venv": str(venv)}), encoding="utf-8")
        (root / f"{kind}-declined").unlink(missing_ok=True)
    except OSError as e:
        return f"FAIL\tpointer\t{_flat(e)}"
    return f"READY\t{venv}\t{engine}"


def check(venv, kind="pdf") -> str:
    venv = Path(venv).resolve()
    st = pyenv.state(venv, kind)
    if st == "READY":
        ready = pyenv._ready(venv)
        if kind == "mpp":
            return f"READY\t{venv}\t{pyenv.mpp_engine_id(ready.get('mpxj'), ready['java']['major'])}"
        return f"READY\t{venv}\t{pyenv.engine_id(ready.get('pypdfium2'), ready.get('pdfium'))}"
    return f"{st}\t{venv}"


def decline(kind="pdf") -> str:
    pyenv._check(kind)
    (cache.root() / f"{kind}-declined").write_text("", encoding="utf-8")
    return "DECLINED"


def remove(venv, kind="pdf") -> str:
    """Delete the venv, only when it is one (holds pyvenv.cfg), and the pointer only when it names this venv."""
    venv = Path(venv).resolve()
    if venv.exists():
        if not (venv.is_dir() and _is_venv(venv) and _replaceable(venv, kind)):
            return _not_venv(venv, kind)
        try:
            shutil.rmtree(venv)
        except OSError as e:
            return f"FAIL\tremove\t{_flat(e)}"
    ptr = cache.root() / f"{kind}-venv.json"
    try:
        named = json.loads(ptr.read_text(encoding="utf-8")).get("venv")
        if isinstance(named, str) and Path(named).resolve() == venv:
            ptr.unlink()
    except (OSError, ValueError, AttributeError):
        pass
    return f"REMOVED\t{venv}"
