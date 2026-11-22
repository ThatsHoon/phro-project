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
from .limits import FIGURES_LATE, OCR_LATE, READ_BYTES, READ_TOKEN_BYTES, READ_TOKENS

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


CODE = "code"  # the folder under the root that keeps code digests (code_digest)
KEYS = "keys"  # the folder under the root that keeps document keys (key)


def _is_hex(text: str) -> bool:
    return len(text) == 40 and all(ch in "0123456789abcdef" for ch in text)


def _memo_get(name: str, m: str) -> str | None:
    try:
        text = (root() / name / m[:2] / m).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError):
        return None
    return text if _is_hex(text) else None


def _memo_put(name: str, m: str, value: str) -> None:
    """Keeps `value` under memo id `m`, written atomically; a disk that cannot be written is no error."""
    f = root() / name / m[:2] / m
    with _lock:
        n = next(_counter)
    tmp = f.parent / f".tmp-{m}-{os.getpid()}-{n}"
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(value, encoding="ascii")
        os.replace(tmp, f)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass


def _sources(top: str) -> list:
    """The *.py and *.json files under top as sorted (relative posix path, absolute path, size, mtime_ns)."""
    found, dirs = [], [top]
    while dirs:
        with os.scandir(dirs.pop()) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    dirs.append(e.path)
                elif os.path.splitext(e.name)[1] in (".py", ".json") and e.is_file():
                    st = e.stat()
                    found.append((os.path.relpath(e.path, top).replace(os.sep, "/"), e.path, st.st_size,
                                  st.st_mtime_ns))
    return sorted(found)


def code_digest(base=None) -> str:
    """sha1 over the reader's own source (sorted relative paths and bytes, CRLF read as LF), so a code change
    misses the cache. Kept in the cache root under a hash of the folder and its files' (path, size, mtime) list,
    and worked out again only when that list changes; the installed package's digest once per process."""
    global _code
    if base is None and _code is not None:
        return _code
    top = os.path.abspath(base if base is not None else os.path.dirname(os.path.abspath(__file__)))
    files = _sources(top)
    listing = hashlib.sha1(os.path.normcase(top).encode("utf-8", "replace") + b"\0")
    for rel, _, size, mtime in files:
        listing.update(f"{rel}\0{size}\0{mtime}\0".encode("utf-8", "replace"))
    m = listing.hexdigest()
    digest = _memo_get(CODE, m)
    if digest is None:
        h = hashlib.sha1()
        for rel, path, _, _ in files:
            h.update(rel.encode("utf-8") + b"\0")
            with open(path, "rb") as f:
                h.update(f.read().replace(b"\r\n", b"\n") + b"\0")
        digest = h.hexdigest()
        _memo_put(CODE, m, digest)
    if base is None:
        _code = digest
    return digest


def key(path, engine: str = "", image: str = "", ocr: str = "") -> str:
    """`engine`: pdf's (native or pdfium) and mpp's (mpxj) engine id. `image`: the figure backend id ("gdiplus",
    "resvg-…" or "none") every other format's key carries, so a file read with no backend is read again once one is
    there. `ocr`: the OCR backend id and version (`gate.ocr_backend()`) every key but mpp's carries, so installing or
    updating an engine reads the file again; empty, the key is the one from before OCR."""
    h = hashlib.sha1(VERSION.encode() + b"\0" + code_digest().encode() + b"\0")
    if engine:
        h.update(engine.encode() + b"\0")
    if image:  # tagged, so an image id never hashes like an engine id
        h.update(b"image:" + image.encode() + b"\0")
    if ocr:
        h.update(b"ocr:" + ocr.encode("utf-8", "replace") + b"\0")
    st = os.stat(path)
    seen = (st.st_size, st.st_mtime_ns, st.st_ino)
    memo = h.copy()
    memo.update(b"file:" + os.path.normcase(os.path.abspath(path)).encode("utf-8", "surrogatepass") + b"\0"
                + repr(seen).encode())
    m = memo.hexdigest()
    k = _memo_get(KEYS, m)
    if k is not None:
        return k
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
        after = os.fstat(f.fileno())
    k = h.hexdigest()
    if (after.st_size, after.st_mtime_ns, after.st_ino) == seen:
        _memo_put(KEYS, m, k)
    return k


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


def partial(d: Path) -> bool:
    """True when the entry at d was filed partial (decision 4): the deadline cut its figures or OCR."""
    import json
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(meta, dict) and meta.get("partial") is True


FIGURES = "figures"  # the folder under the root that holds converted figures (Figures)
_FIG_MAGIC = b"PHROFIG1"


class Figures:
    """Converted figures, one file each at <dir>/<k[:2]>/<k>: the PNG (if any), the text lines and the figure's own
    warnings. A read the deadline cut is filed partial, and the figures it finished are kept here, so another read
    of the same pictures takes them and gets further. Figures cut by the time are never stored. Written atomically;
    prune clears them."""

    def __init__(self, dir: Path):
        self.dir = Path(dir)

    @staticmethod
    def key(sha: str, codepage: str, page: int, backend: str) -> str:
        """sha1 of the picture bytes' sha1, the code page, the page and the backend id, under the reader's version
        and code digest (a code change misses, as the document cache does)."""
        h = hashlib.sha1(VERSION.encode() + b"\0" + code_digest().encode() + b"\0")
        for part in (sha, codepage, str(int(page)), backend):
            h.update(part.encode("utf-8", "replace") + b"\0")
        return h.hexdigest()

    def path(self, k: str) -> Path:
        return self.dir / k[:2] / k

    def get(self, k: str):
        """The stored raster.Figure, or None (a miss, or a file that does not read back whole)."""
        import json
        from .raster import Figure
        f = self.path(k)
        try:
            raw = f.read_bytes()
        except OSError:
            return None
        try:
            if raw[:8] != _FIG_MAGIC or len(raw) < 12:
                return None
            n = int.from_bytes(raw[8:12], "little")
            head = json.loads(raw[12:12 + n].decode("utf-8"))
            png = raw[12 + n:] or None
            lines, warnings = head["lines"], head["warnings"]
            if (not isinstance(lines, list) or not all(isinstance(x, str) for x in lines)
                    or not isinstance(warnings, list) or not all(isinstance(x, str) for x in warnings)
                    or head.get("png") != len(png or b"") or (png is not None and png[:8] != b"\x89PNG\r\n\x1a\n")):
                return None
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
        try:
            os.utime(f, None)  # in use: prune keeps it
        except OSError:
            pass
        return Figure(png, lines, set(warnings))

    def put(self, k: str, fig) -> None:
        """Stores a finished figure; one cut by the time (FIGURES_LATE) is not stored. Errors are swallowed."""
        import json
        if FIGURES_LATE in fig.warnings:
            return
        png = bytes(fig.png) if fig.png is not None else b""
        head = json.dumps({"lines": list(fig.lines), "warnings": sorted(fig.warnings), "png": len(png)},
                          ensure_ascii=False).encode("utf-8")
        f = self.path(k)
        with _lock:
            n = next(_counter)
        tmp = f.parent / f".tmp-{k}-{os.getpid()}-{n}"
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(_FIG_MAGIC + len(head).to_bytes(4, "little") + head + png)
            os.replace(tmp, f)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


OCR = "ocr"  # the folder under the root that holds per-picture OCR results (OcrResults)


class OcrResults:
    """Per-picture OCR results, one JSON file each at <dir>/<k[:2]>/<k>, keyed by `ocr.picture_key`: the worker's
    answer fields (page, rules, unread, hint). The verdict is not stored: the Session judges a loaded result again,
    so a calibrated threshold applies to it. A read the deadline cut keeps the pictures it finished here, so another
    read of the same pictures continues (decision 12). Written atomically; prune clears them as it does figures."""

    def __init__(self, dir: Path):
        self.dir = Path(dir)

    def path(self, k: str) -> Path:
        return self.dir / k[:2] / k

    def get(self, k: str) -> dict | None:
        """The stored dict, or None (a miss, or a file that is not a JSON object within OCR_FRAME_JSON bytes)."""
        import json
        from . import limits
        f = self.path(k)
        try:
            if f.stat().st_size > limits.OCR_FRAME_JSON:
                return None
            raw = f.read_bytes()
        except OSError:
            return None
        try:
            d = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            return None
        if not isinstance(d, dict):
            return None
        try:
            os.utime(f, None)  # in use: prune keeps it
        except OSError:
            pass
        return d

    def put(self, k: str, result: dict) -> None:
        """Stores `result`; errors (a value that is not JSON, a disk that cannot be written) are swallowed."""
        import json
        try:
            data = json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (TypeError, ValueError, RecursionError):
            return
        f = self.path(k)
        with _lock:
            n = next(_counter)
        tmp = f.parent / f".tmp-{k}-{os.getpid()}-{n}"
        try:
            f.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            os.replace(tmp, f)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass


def store(k: str, src, hidden: bool = False, again: bool = False, **opts) -> Path:
    """Converts src into the entry for k and returns it; an entry already there is returned as is, or, with
    `again`, put aside for the new one once that is done."""
    from . import images
    bucket = root() / k[:2]
    target = bucket / k
    if _complete(target) and not again:
        return target
    with _lock:
        n = next(_counter)
    tmp = bucket / f".tmp-{k}-{os.getpid()}-{n}"
    old = images.FIGURE_CACHE
    images.FIGURE_CACHE = Figures(root() / FIGURES)  # the figures of this read are taken from and kept there
    try:
        meta = convert(src, tmp, hidden, **opts)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        images.FIGURE_CACHE = old
    extra = {}
    if {FIGURES_LATE, OCR_LATE} & set(meta.get("warnings") or []):
        extra["partial"] = True
    if (tmp / "out.md").stat().st_size > min(READ_TOKENS * READ_TOKEN_BYTES, READ_BYTES):
        lines = (tmp / "out.md").read_bytes().split(b"\n")
        extra["line_bytes"] = [len(x) + 1 for x in (lines[:-1] if lines[-1] == b"" else lines)]
    if extra:
        import json
        meta.update(extra)
        (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    if again and target.exists():
        old_dir = bucket / f".tmp-old-{k}-{os.getpid()}-{n}"
        try:
            os.replace(target, old_dir)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            return target
        try:
            os.replace(tmp, target)
        except OSError:
            shutil.rmtree(tmp, ignore_errors=True)
            try:
                os.replace(old_dir, target)
            except OSError:
                pass
            return target
        shutil.rmtree(old_dir, ignore_errors=True)
        return target
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
    for name in (FIGURES, OCR, CODE, KEYS):
        # converted figures and OCR results: unused for `days` (a lookup touches them), or a writer's leftover
        # after a day
        files = base / name
        if not files.is_dir():
            continue
        for bucket in files.iterdir():
            if not bucket.is_dir():
                continue
            for f in bucket.iterdir():
                try:
                    age = f.stat().st_mtime
                    if age < (time.time() - 86400 if f.name.startswith(".tmp-") else cutoff):
                        f.unlink()
                        n += 1
                except OSError:
                    pass
    for bucket in base.iterdir():
        if not bucket.is_dir() or bucket.name in ("offers", "failed", "pdf-declined", "mpp-declined", FIGURES, OCR,
                                                  CODE, KEYS):
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
