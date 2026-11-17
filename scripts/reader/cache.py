"""Content-hash cache: entries live at <root>/<sha1[:2]>/<sha1>/ (out.md, assets/, meta.json)."""
import hashlib
import itertools
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from pathlib import Path

from . import VERSION, convert

_counter = itertools.count()
_lock = threading.Lock()
_code = None
_private = None  # this process's own cache root when the shared fallback is unsafe


def _configured() -> Path:
    env = os.environ.get("PHRO_READER_CACHE")
    if env:
        return Path(env)
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "phro-project" / "reader"
    return Path.home() / ".cache" / "phro-project" / "reader"


def venv_base() -> Path:
    """The folder that holds `pdf-venv`: the parent of the configured cache root, or the root itself when the
    cache fell back to the temp dir (its parent there is the shared temp dir)."""
    r = root()
    return r.parent if r == _configured() else r


def root() -> Path:
    base = _configured()
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except OSError:
        return _fallback()


def _fallback() -> Path:
    """A cache root in the temp dir. On POSIX, /tmp is shared between users, so the directory is per user, 0700,
    owned by this user and not a symlink; failing that, a fresh private directory for this process only."""
    global _private
    tmp = Path(tempfile.gettempdir())
    getuid = getattr(os, "getuid", None)
    if getuid is None:  # Windows: the temp dir is already per user
        fb = tmp / "phro-project-reader"
        fb.mkdir(parents=True, exist_ok=True)
        return fb
    fb = tmp / f"phro-project-reader-{getuid()}"
    try:
        fb.mkdir(mode=0o700, exist_ok=True)
        st = os.lstat(fb)
        if stat.S_ISDIR(st.st_mode) and st.st_uid == getuid():  # lstat: a symlink is not S_ISDIR
            if st.st_mode & 0o077:
                os.chmod(fb, 0o700)
            return fb
    except OSError:
        pass
    if _private is None:  # mkdtemp makes it 0700; entries there are not found by later processes
        _private = Path(tempfile.mkdtemp(prefix="phro-project-reader-", dir=tmp))
    return _private


def code_digest(base=None) -> str:
    """sha1 over the reader's own source (sorted relative paths and bytes, CRLF read as LF), so a code change
    misses the cache; the installed package's digest is worked out once per process."""
    global _code
    if base is None and _code is not None:
        return _code
    top = Path(base) if base is not None else Path(__file__).resolve().parent
    h = hashlib.sha1()
    files = [f for f in top.rglob("*") if f.suffix in (".py", ".json") and f.is_file()]
    for f in sorted(files, key=lambda q: q.relative_to(top).as_posix()):
        h.update(f.relative_to(top).as_posix().encode("utf-8") + b"\0")
        h.update(f.read_bytes().replace(b"\r\n", b"\n") + b"\0")
    if base is None:
        _code = h.hexdigest()
    return h.hexdigest()


def key(path, engine: str = "") -> str:
    h = hashlib.sha1(VERSION.encode() + b"\0" + code_digest().encode() + b"\0")
    if engine:  # pdf (native or pdfium) and mpp (mpxj) have one; every other format's key is unchanged
        h.update(engine.encode() + b"\0")
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def lookup(k: str):
    d = root() / k[:2] / k
    if (d / "out.md").is_file() and (d / "meta.json").is_file():
        try:
            os.utime(d / "meta.json", None)
        except OSError:
            pass
        return d
    return None


def _complete(d: Path) -> bool:
    return (d / "out.md").is_file() and (d / "meta.json").is_file()


def store(k: str, src, hidden: bool = False, **opts) -> Path:
    bucket = root() / k[:2]
    target = bucket / k
    if _complete(target):
        return target
    with _lock:
        n = next(_counter)
    tmp = bucket / f".tmp-{k}-{os.getpid()}-{n}"
    try:
        convert(src, tmp, hidden, **opts)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    for attempt in (0, 1):
        try:
            os.replace(tmp, target)
            return target
        except OSError:
            # on Windows replacing onto an existing directory fails
            if _complete(target):  # another writer won
                shutil.rmtree(tmp, ignore_errors=True)
                return target
            if attempt == 0 and target.exists():  # incomplete leftover: clear and retry once
                shutil.rmtree(target, ignore_errors=True)
                continue
            shutil.rmtree(tmp, ignore_errors=True)
            raise


def prune(days: int = 30) -> int:
    cutoff = time.time() - days * 86400
    n = 0
    base = root()
    for name in ("offers", "failed"):  # markers of the pdf venv offer and of failed installs: a day is enough
        marks = base / name
        if not marks.is_dir():
            continue
        for f in marks.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < time.time() - 86400:
                    f.unlink()
            except OSError:
                pass
    for bucket in base.iterdir():
        if not bucket.is_dir() or bucket.name in ("offers", "failed", "pdf-declined", "mpp-declined"):
            continue
        for d in bucket.iterdir():
            if d.name.startswith(".tmp-"):
                try:
                    stale = d.stat().st_mtime < time.time() - 86400
                except OSError:
                    continue
                if stale:
                    shutil.rmtree(d, ignore_errors=True)
                    n += 1
                continue
            try:
                old = (d / "meta.json").stat().st_mtime < cutoff
            except OSError:
                continue
            if old:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
    return n
