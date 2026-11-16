"""State of the optional venvs (kind "pdf": pypdfium2; kind "mpp": MPXJ and JPype, with a Java found on PATH):
cheap checks only (no subprocess at import, no format modules).

A venv is made only by `read.py setup` (see pysetup). Here: where it lives, whether it is usable, and
the engine id a file is read with. `java_info` runs `java` once, for setup (the subprocess module is imported
inside it, so importing this module stays light).
"""
import base64
import hashlib
import json
import os
import re
import sys
from pathlib import Path

KINDS = ("pdf", "mpp")
_HERE = Path(__file__).resolve().parent
REQUIREMENTS = {"pdf": _HERE / "requirements-pdf.txt", "mpp": _HERE / "requirements-mpp.txt"}
_ENV = {"pdf": "PHRO_READER_PYENV", "mpp": "PHRO_READER_MPPENV"}
_DISTS = {"pdf": ("pypdfium2",), "mpp": ("mpxj", "jpype1")}

JAVA_FLOOR = 8  # MPXJ 16.10.0 compiles for Java 8; JPype 1.7.1 lists Java 8
MPXJ_VERSION = "16.10.0"
JAVA_TIMEOUT = 30
# where a JDK or JRE keeps its JVM library under java.home, in the order tried
JVM_LIBRARIES = (
    ("bin", "server", "jvm.dll"), ("bin", "client", "jvm.dll"),
    ("lib", "server", "libjvm.so"), ("lib", "server", "libjvm.dylib"),
    ("jre", "bin", "server", "jvm.dll"), ("jre", "bin", "client", "jvm.dll"),
    ("jre", "lib", "server", "libjvm.so"), ("jre", "lib", "amd64", "server", "libjvm.so"),
    ("jre", "lib", "aarch64", "server", "libjvm.so"), ("jre", "lib", "server", "libjvm.dylib"),
)


def _check(kind):
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")


def _requirements(kind) -> Path:
    _check(kind)
    return REQUIREMENTS[kind]


def forced_native(kind="pdf") -> bool:
    """The kind's env var is `off`: pdf reads natively, mpp is not read (engine() is "")."""
    _check(kind)
    return os.environ.get(_ENV[kind]) == "off"


def venv_dir(kind="pdf") -> Path:
    _check(kind)
    env = os.environ.get(_ENV[kind])
    if env and env != "off":
        return Path(env)
    data = os.environ.get("CLAUDE_PLUGIN_DATA")
    if data:
        return Path(data) / f"{kind}-venv"
    from . import cache
    root = cache.root()
    try:
        ptr = json.loads((root / f"{kind}-venv.json").read_text(encoding="utf-8"))
        if isinstance(ptr, dict) and isinstance(ptr.get("venv"), str) and ptr["venv"]:
            return Path(ptr["venv"])
    except (OSError, ValueError):
        pass
    return cache.venv_base() / f"{kind}-venv"


def interpreter(venv) -> Path:
    venv = Path(venv)
    if sys.platform == "win32":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python3"


def requirements_hash(kind="pdf") -> str:
    return hashlib.sha256(_requirements(kind).read_bytes()).hexdigest()


def _ready(venv):
    try:
        data = json.loads((Path(venv) / "ready.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def ready_kind(ready) -> str:
    """The kind a ready.json names; one without the field is a 2d pdf venv."""
    return ready.get("kind", "pdf") if isinstance(ready, dict) else "pdf"


def _home(venv):
    try:
        for line in (Path(venv) / "pyvenv.cfg").read_text(encoding="utf-8").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip().lower() == "home":
                return v.strip()
    except OSError:
        pass
    return None


def find_java():
    """The first `java` on the absolute entries of PATH, as an absolute path, else None. shutil.which and a bare
    name given to CreateProcess both look in the current directory first on Windows, so PATH is walked here:
    empty, "." and other relative entries are skipped."""
    exts = [e for e in os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e] \
        if sys.platform == "win32" else [""]
    for entry in os.environ.get("PATH", "").split(os.pathsep):
        entry = entry.strip().strip('"')
        if not entry or not os.path.isabs(entry):
            continue
        for ext in exts:
            cand = os.path.join(entry, "java" + ext)
            if os.path.isfile(cand):
                return cand
    return None


def _same_path(a, b) -> bool:
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


def _java_ok(ready) -> bool:
    """The recorded Java is still the java on PATH, and its JVM library is still there."""
    java = ready.get("java")
    if not isinstance(java, dict) or not isinstance(java.get("major"), int):
        return False
    path, jvm = java.get("path"), java.get("jvm")
    if not isinstance(path, str) or not isinstance(jvm, str):
        return False
    found = find_java()
    try:
        return bool(found) and _same_path(found, path) and Path(jvm).is_file()
    except (OSError, ValueError):
        return False


def state(venv, kind="pdf") -> str:
    _check(kind)
    venv = Path(venv)
    if not venv.is_dir():
        return "MISSING"
    if not interpreter(venv).is_file():
        return "BROKEN"
    ready = _ready(venv)
    if ready is None or ready_kind(ready) != kind:
        return "BROKEN"
    home = _home(venv)
    if not home or not Path(home).is_dir():
        return "BROKEN"
    if ready.get("requirements") != requirements_hash(kind):
        return "STALE"
    if kind == "mpp" and not _java_ok(ready):
        return "STALE"
    return "READY"


def engine_id(version, build) -> str:
    return f"pdfium-{version}-{build}"


def mpp_engine_id(mpxj: str, major: int) -> str:
    return f"mpxj-{mpxj}-java{major}"


def engine(kind="pdf", venv=None) -> str:
    """pdf: the engine id when READY, else "native". mpp: the engine id when READY, else ""."""
    _check(kind)
    if forced_native(kind):
        return "native" if kind == "pdf" else ""
    off = "native" if kind == "pdf" else ""
    venv = Path(venv) if venv is not None else venv_dir(kind)
    if state(venv, kind) != "READY":
        return off
    ready = _ready(venv)
    if kind == "mpp":
        major = (ready.get("java") or {}).get("major")
        if not ready.get("mpxj") or not isinstance(major, int):
            return ""
        return mpp_engine_id(ready["mpxj"], major)
    if not ready.get("pypdfium2") or not ready.get("pdfium"):
        return "native"
    return engine_id(ready["pypdfium2"], ready["pdfium"])


def engine_id_from_module(kind="pdf") -> str:
    """Used in the venv child, where the libraries import."""
    _check(kind)
    if kind == "mpp":
        import importlib.metadata
        ready = json.loads((Path(sys.prefix) / "ready.json").read_text(encoding="utf-8"))
        return mpp_engine_id(importlib.metadata.version("mpxj"), ready["java"]["major"])
    from pypdfium2 import version as v
    return engine_id(v.PYPDFIUM_INFO.version, v.PDFIUM_INFO.build)


def _norm(name) -> str:
    return name.lower().replace("_", "-")


def _records(venv, dist="pypdfium2") -> list:
    """The RECORD files of every dist-info of `dist` under the venv (a pypdfium2 wheel has one; it lists
    pypdfium2_raw too)."""
    found = []
    for rec in Path(venv).rglob("RECORD"):
        info = rec.parent.name
        if info.endswith(".dist-info") and _norm(info[:-len(".dist-info")].rpartition("-")[0]) == _norm(dist):
            found.append(rec)
    return found


def record_digests(venv, kind="pdf") -> dict:
    """{dist: sha256 hex of its RECORD file} for each package of the kind; empty unless every one has exactly one."""
    out = {}
    for dist in _DISTS[kind]:
        recs = _records(venv, dist)
        if len(recs) != 1:
            return {}
        out[dist] = hashlib.sha256(recs[0].read_bytes()).hexdigest()
    return out


def _verify_record(venv, dist, want, needs=None) -> bool:
    """The one RECORD of `dist` hashes to `want`, lists a file under `needs` (when given), and every file it lists
    with a digest (paths are relative to site-packages, so `../../Scripts/...` counts) hashes to it."""
    recs = _records(venv, dist)
    if len(recs) != 1:
        return False
    rec = recs[0]
    try:
        data = rec.read_bytes()
    except OSError:
        return False
    if hashlib.sha256(data).hexdigest() != want:
        return False
    base = rec.parent.parent
    seen = needs is None
    for line in data.decode("utf-8", "replace").splitlines():
        parts = line.rsplit(",", 2)
        if len(parts) != 3:
            continue
        if needs and parts[0].startswith(needs):
            seen = True
        if not parts[1].startswith("sha256="):
            continue
        digest = parts[1][len("sha256="):]
        try:
            got = base64.urlsafe_b64encode(hashlib.sha256((base / parts[0]).read_bytes()).digest())
        except OSError:
            return False
        if got.rstrip(b"=").decode("ascii") != digest.rstrip("="):
            return False
    return seen


def verify(venv, kind="pdf") -> bool:
    """The full check, for every package of the kind (see _verify_record); mpp adds the cheap Java check."""
    _check(kind)
    ready = _ready(venv)
    if ready is None or not isinstance(ready.get("records"), dict) or ready_kind(ready) != kind:
        return False
    if kind == "mpp":
        if not _java_ok(ready):
            return False
        return all(_verify_record(venv, d, ready["records"].get(d)) for d in _DISTS["mpp"])
    return _verify_record(venv, "pypdfium2", ready["records"].get("pypdfium2"), needs="pypdfium2_raw/")


def declined(kind="pdf") -> bool:
    _check(kind)
    from . import cache
    return (cache.root() / f"{kind}-declined").exists()


def python_bits() -> int:
    return 64 if sys.maxsize > 2**32 else 32


def java_major(version: str):
    """"1.8.0_392" -> 8, "21.0.5" -> 21, "17" -> 17, "21-ea" -> 21, anything else None."""
    m = re.match(r"(\d+)(?:\.(\d+))?", version.strip()) if isinstance(version, str) else None
    if not m:
        return None
    first = int(m.group(1))
    if first == 1 and m.group(2) is not None:
        return int(m.group(2))
    return first


def _props(text) -> dict:
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition(" = ")
        if sep and k.strip() and not k.strip().count(" "):
            out.setdefault(k.strip(), v.strip())
    return out


def java_info(run=None):
    """Run `java -XshowSettings:properties -version`: {"path", "major", "home", "jvm", "bits"}, or the FAIL detail."""
    path = find_java()
    if not path:
        return "java not found on PATH"
    if run is None:
        import subprocess
        run = subprocess.run
    try:
        r = run([path, "-XshowSettings:properties", "-version"], capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=JAVA_TIMEOUT)
    except Exception as e:  # noqa: BLE001 - not runnable, or past the timeout
        return f"java did not run ({type(e).__name__})"
    props = _props((r.stderr or "") + "\n" + (r.stdout or ""))
    major = java_major(props.get("java.version", ""))
    if major is None:
        return "could not read the Java version"
    if major < JAVA_FLOOR:
        return f"Java {major} found; MPXJ needs Java {JAVA_FLOOR} or later"
    bits = props.get("sun.arch.data.model")
    bits = int(bits) if bits in ("32", "64") else (64 if "64" in props.get("os.arch", "") else 32)
    if bits != python_bits():
        return f"{bits}-bit Java with {python_bits()}-bit Python"
    home = props.get("java.home", "")
    jvm = next((str(Path(home).joinpath(*rel)) for rel in JVM_LIBRARIES
                if home and Path(home).joinpath(*rel).is_file()), None)
    if jvm is None:
        return f"no JVM library under {home}"
    return {"path": path, "major": major, "home": home, "jvm": jvm, "bits": bits}
