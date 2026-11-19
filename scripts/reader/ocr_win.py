"""Windows OCR (Windows.Media.Ocr) through ctypes WinRT COM, in process: no package and no PowerShell.

Setup is RoInitialize (multithreaded) and RoGetActivationFactory for OcrEngine, Language, SoftwareBitmap and
CryptographicBuffer; strings are HSTRINGs. Pixels reach the engine as a SoftwareBitmap copied from a buffer of the
reader's own Gray8 bytes, so no OS image decoder sees document bytes. Every interface is called through its vtable:
slots 0-2 are IUnknown, 3-5 IInspectable, and the interface's own methods follow from slot 6 in metadata order. Every
pointer is released once. ctypes is imported only inside functions, so importing this module stays light and works
off Windows.
"""
import math
import os
import sys
import threading
import time
from pathlib import Path

from .ocr import OcrPage, Word
from .ocr_pixels import Gray

# IIDs, pinned from the Windows metadata (C:\Windows\System32\WinMetadata\*.winmd, the metadata the Windows SDK
# publishes) and checked against the running system by QueryInterface. The other interfaces used here (IOcrEngine,
# IOcrResult, IOcrLine, IOcrWord, ILanguage, IAsyncOperation<OcrResult>, IVectorView<T>, IReference<double>) are only
# reached as the typed pointers their getters return, so no QueryInterface needs their IIDs.
_IID_OCR_ENGINE_STATICS = "5bffa85a-3384-3540-9940-699120d428a8"  # Windows.Media.Ocr.IOcrEngineStatics
_IID_LANGUAGE_FACTORY = "9b0252ac-0c27-44f8-b792-9793fb66c63e"  # Windows.Globalization.ILanguageFactory
_IID_SOFTWARE_BITMAP_STATICS = "df0385db-672f-4a9d-806e-c2442f343e86"  # Windows.Graphics.Imaging.ISoftwareBitmapStatics
_IID_CRYPTO_BUFFER_STATICS = "320b7e22-3cb0-4cdf-8663-1d28910065eb"  # ...Cryptography.ICryptographicBufferStatics
_IID_ASYNC_INFO = "00000036-0000-0000-c000-000000000046"  # Windows.Foundation.IAsyncInfo

_GRAY8 = 62  # BitmapPixelFormat.Gray8
_ALPHA_IGNORE = 2  # BitmapAlphaMode.Ignore
_STARTED, _COMPLETED, _ERROR = 0, 1, 3  # AsyncStatus (2 is Canceled)
_RO_INIT_MULTITHREADED = 1
_RPC_E_CHANGED_MODE = 0x80010106 - (1 << 32)  # the thread is already single-threaded: accepted
_POLL = 0.005  # seconds between Status reads in recognize

_lib = None
_LOCK = threading.Lock()


def recognizers() -> list[str]:
    """The recognizer folders under %WINDIR%\\OCR that hold an .orp file, lower case, found by listing only."""
    if sys.platform != "win32":
        return []
    names = []
    try:
        with os.scandir(_ocr_root()) as it:
            folders = [e for e in it if e.is_dir()]
    except OSError:
        return []
    for d in folders:
        try:
            with os.scandir(d.path) as files:
                if any(f.name.lower().endswith(".orp") and f.is_file() for f in files):
                    names.append(d.name.lower())
        except OSError:
            continue
    return sorted(names)


class WinOcr:
    """Windows OCR for one language: the factories, the Language and a pool of OcrEngine instances, held until close().

    An OcrEngine runs one recognition at a time: a RecognizeAsync on a busy engine fails at once with E_ABORT, and a
    recognition cancelled after it began keeps its engine busy until the page is done. So each operation takes an idle
    engine from the pool, or a new one (about 1 MB; creating it takes milliseconds), and gives it back once its result
    is read. An engine whose operation was cancelled mid-page is released, not reused. Several operations can be
    started before any finishes, from any thread; the caller bounds how many. close() comes after the last one.
    """

    def __init__(self, lang: str = "ko"):
        lib = _rt()
        ct = lib.ct
        self._lib = lib
        self._statics = self._bitmaps = self._buffers = self._language = None
        self._idle: list[int] = []
        self._lock = threading.Lock()
        self._closed = False
        try:
            self._statics = _factory(lib, "Windows.Media.Ocr.OcrEngine", _IID_OCR_ENGINE_STATICS)
            self._bitmaps = _factory(lib, "Windows.Graphics.Imaging.SoftwareBitmap", _IID_SOFTWARE_BITMAP_STATICS)
            self._buffers = _factory(lib, "Windows.Security.Cryptography.CryptographicBuffer",
                                     _IID_CRYPTO_BUFFER_STATICS)
            self.max_dim = _get(lib, self._statics, 6, "OcrEngine.MaxImageDimension", ct.c_uint32)
            if self.max_dim <= 0:
                raise OSError("Windows OCR reports no MaxImageDimension")
            self._language = _language(lib, lang)
            ok = ct.c_ubyte(0)
            _call(lib, self._statics, 8, "OcrEngine.IsLanguageSupported", (ct.c_void_p, ct.POINTER(ct.c_ubyte)),
                  self._language, ct.byref(ok))
            engine = self._new_engine() if ok.value else None
            own = engine is not None
            if not own:  # no recognizer for `lang`: the engine of the user-profile languages, for every operation
                _release(lib, self._language)
                self._language = None
                engine = self._new_engine()
            self._idle.append(engine)
            tag = _tag(lib, engine)
            self.engine = f"win-{lang.lower()}" if own else f"win-{tag.lower()}"
            self.version = _version(tag)
        except BaseException:
            self.close()
            raise

    def _new_engine(self) -> int | None:
        """A new OcrEngine for the Language (None when Windows gives none), or for the user-profile languages when
        there is no Language (OSError when Windows gives none)."""
        lib, ct = self._lib, self._lib.ct
        if self._language:
            return _out_ptr(lib, self._statics, 9, "OcrEngine.TryCreateFromLanguage", (ct.c_void_p,), self._language)
        engine = _out_ptr(lib, self._statics, 10, "OcrEngine.TryCreateFromUserProfileLanguages", ())
        if not engine:
            raise OSError("Windows OCR has no recognizer for the user's languages")
        return engine

    def _take(self) -> int:
        with self._lock:
            if self._closed:
                raise OSError("Windows OCR is closed")
            if self._idle:
                return self._idle.pop()
        engine = self._new_engine()
        if not engine:
            raise OSError("Windows OCR gave no engine")
        return engine

    def _give(self, engine: int, reuse: bool):
        """Back to the pool when it is free for another page; released otherwise."""
        with self._lock:
            if reuse and not self._closed:
                self._idle.append(engine)
                return
        _release(self._lib, engine)

    def start(self, g: Gray) -> "WinOp":
        """Starts RecognizeAsync on `g`, fitted to max_dim on its long side by keeping every step-th row and column."""
        lib = _rt()
        ct = lib.ct
        long = max(g.w, g.h)
        step = -(-long // self.max_dim) if long > self.max_dim else 1
        fw, fh, data = _fit(g, step)
        engine = self._take()
        buf = bitmap = op = None
        try:
            buf = _out_ptr(lib, self._buffers, 9, "CryptographicBuffer.CreateFromByteArray",
                           (ct.c_uint32, ct.c_char_p), len(data), data)
            bitmap = _out_ptr(lib, self._bitmaps, 10, "SoftwareBitmap.CreateCopyFromBuffer",
                              (ct.c_void_p, ct.c_int32, ct.c_int32, ct.c_int32, ct.c_int32),
                              buf, _GRAY8, fw, fh, _ALPHA_IGNORE)
            op = _out_ptr(lib, engine, 6, "OcrEngine.RecognizeAsync", (ct.c_void_p,), bitmap)
            if not op:
                raise OSError("RecognizeAsync gave no operation")
            info = _qi(lib, op, _IID_ASYNC_INFO, "IAsyncInfo")
            started = WinOp(self, engine, op, info, g.w, g.h, step)
            engine = op = None  # owned by `started` now
            return started
        finally:
            if engine:
                self._give(engine, reuse=op is None)  # an operation that began may still hold it
            _release(lib, op)
            _release(lib, bitmap)
            _release(lib, buf)

    def recognize(self, g: Gray, deadline: float) -> OcrPage | None:
        """`g` read to the end, or None when time.monotonic() reaches `deadline` first (the operation is cancelled)."""
        op = self.start(g)
        try:
            while not op.done():
                if time.monotonic() >= deadline:
                    op.cancel()
                    return None
                time.sleep(_POLL)
            return op.result()
        except BaseException:
            op.cancel()
            raise

    def close(self):
        """Releases the idle engines and the factories. An engine still in an operation is released when it ends."""
        lock = getattr(self, "_lock", None)
        if lock is None:
            return
        with lock:
            self._closed = True
            idle, self._idle = self._idle, []
        for p in idle:
            _release(self._lib, p)
        for name in ("_language", "_statics", "_bitmaps", "_buffers"):
            p = getattr(self, name, None)
            setattr(self, name, None)
            _release(self._lib, p)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001 - interpreter shutdown: nothing left to release into
            pass


class WinOp:
    """One RecognizeAsync in flight on its own engine. done() polls it; result() or cancel() ends it and releases it."""

    def __init__(self, owner: WinOcr, engine: int, op: int, info: int, w: int, h: int, step: int):
        self._owner, self._engine, self._op, self._info = owner, engine, op, info
        self._w, self._h, self._step = w, h, step

    def done(self) -> bool:
        if not self._op:
            return True
        return self._status(_rt()) != _STARTED

    def _status(self, lib) -> int:
        return _get(lib, self._info, 7, "IAsyncInfo.Status", lib.ct.c_int32)

    def result(self) -> OcrPage:
        """The words in engine order, boxes in the frame of the bitmap given to start(). Ends the operation."""
        if not self._op:
            raise OSError("the OCR operation has already ended")
        lib = _rt()
        res = status = None
        try:
            status = self._status(lib)
            if status == _ERROR:
                code = _get(lib, self._info, 8, "IAsyncInfo.ErrorCode", lib.ct.c_int32)
                raise OSError(f"RecognizeAsync failed: HRESULT 0x{code & 0xFFFFFFFF:08X}")
            if status != _COMPLETED:
                raise OSError(f"RecognizeAsync not completed (status {status})")
            res = _out_ptr(lib, self._op, 8, "IAsyncOperation<OcrResult>.GetResults", ())
            if not res:
                raise OSError("RecognizeAsync gave no result")
            return self._page(lib, res)
        finally:
            _release(lib, res)
            self._end(lib, close=status is not None and status != _STARTED, reuse=status == _COMPLETED)

    def _page(self, lib, res) -> OcrPage:
        ct = lib.ct
        angle = 0.0
        ref = _out_ptr(lib, res, 7, "OcrResult.TextAngle", ())  # IReference<double>, null when there is none
        if ref:
            try:
                value = _get(lib, ref, 6, "IReference<double>.Value", ct.c_double)
                angle = value if math.isfinite(value) else 0.0
            finally:
                _release(lib, ref)
        words = []
        lines = _out_ptr(lib, res, 6, "OcrResult.Lines", ())  # IVectorView<OcrLine>
        try:
            for n in range(_size(lib, lines) if lines else 0):
                line = _out_ptr(lib, lines, 6, "Lines.GetAt", (ct.c_uint32,), n)
                vec = None
                try:
                    vec = _out_ptr(lib, line, 6, "OcrLine.Words", ())  # IVectorView<OcrWord>
                    for i in range(_size(lib, vec) if vec else 0):
                        word = _out_ptr(lib, vec, 6, "Words.GetAt", (ct.c_uint32,), i)
                        try:
                            rect = lib.Rect()
                            _call(lib, word, 6, "OcrWord.BoundingRect", (ct.POINTER(lib.Rect),), ct.byref(rect))
                            text = _string(lib, word, 7, "OcrWord.Text")
                        finally:
                            _release(lib, word)
                        box = self._box(rect)
                        if text and box:
                            words.append(Word(text, *box, line=n, conf=None))
                finally:
                    _release(lib, vec)
                    _release(lib, line)
        finally:
            _release(lib, lines)
        owner = self._owner
        return OcrPage(self._w, self._h, words, angle, 0, owner.engine, owner.version)

    def _box(self, r) -> tuple[int, int, int, int] | None:
        """The engine's float rect scaled back by the fit step, as a pixel box of at least 1x1 inside the frame."""
        if not all(math.isfinite(v) for v in (r.X, r.Y, r.Width, r.Height)):
            return None
        s, w, h = self._step, self._w, self._h
        x0 = min(max(math.floor(r.X * s), 0), w - 1)
        y0 = min(max(math.floor(r.Y * s), 0), h - 1)
        x1 = min(max(math.ceil((r.X + r.Width) * s), x0 + 1), w)
        y1 = min(max(math.ceil((r.Y + r.Height) * s), y0 + 1), h)
        return x0, y0, x1 - x0, y1 - y0

    def cancel(self):
        """Cancels the operation if it still runs, and releases it. Safe to call more than once."""
        if not self._op:
            return
        lib = _rt()
        status = None
        try:
            status = self._status(lib)
            if status == _STARTED:
                _call(lib, self._info, 9, "IAsyncInfo.Cancel", ())
        except OSError:
            pass
        finally:
            # a recognition that had begun keeps its engine busy after Cancel: only a finished one's engine is reused
            self._end(lib, close=False, reuse=status == _COMPLETED)

    def _end(self, lib, close: bool, reuse: bool):
        op, info, engine = self._op, self._info, self._engine
        self._op = self._info = self._engine = None
        try:
            if close and info:
                _call(lib, info, 10, "IAsyncInfo.Close", ())
        except OSError:
            pass
        finally:
            _release(lib, info)
            _release(lib, op)
            if engine:
                self._owner._give(engine, reuse)

    def __del__(self):
        try:
            self.cancel()
        except Exception:  # noqa: BLE001 - interpreter shutdown: nothing left to release into
            pass


# ---- WinRT plumbing --------------------------------------------------------------------------------------------------

def _rt():
    """combase's WinRT functions, typed once a process, with this thread initialized (multithreaded) once; OSError
    when WinRT cannot be loaded."""
    global _lib
    if _lib is None:
        with _LOCK:
            if _lib is None:
                try:
                    _lib = _load()
                except (OSError, AttributeError, ImportError) as e:  # no combase, or no ctypes.WinDLL off Windows
                    _lib = f"WinRT unavailable: {e}"
    if isinstance(_lib, str):
        raise OSError(_lib)
    lib = _lib
    if not getattr(lib.local, "ready", False):
        hr = lib.RoInitialize(_RO_INIT_MULTITHREADED)
        if hr < 0 and hr != _RPC_E_CHANGED_MODE:
            raise OSError(f"RoInitialize: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
        lib.local.ready = True
    return lib


def _load():
    import ctypes
    import uuid
    from ctypes import POINTER, Structure, c_float, c_int32, c_long, c_ubyte, c_uint16, c_uint32, c_void_p, c_wchar_p

    class GUID(Structure):
        _fields_ = [("Data1", c_uint32), ("Data2", c_uint16), ("Data3", c_uint16), ("Data4", c_ubyte * 8)]

    class Rect(Structure):  # Windows.Foundation.Rect
        _fields_ = [("X", c_float), ("Y", c_float), ("Width", c_float), ("Height", c_float)]

    combase = ctypes.WinDLL("combase")
    PP = POINTER(c_void_p)
    sigs = {
        "RoInitialize": (c_long, [c_int32]),
        "RoGetActivationFactory": (c_long, [c_void_p, POINTER(GUID), PP]),
        "WindowsCreateString": (c_long, [c_wchar_p, c_uint32, PP]),
        "WindowsDeleteString": (c_long, [c_void_p]),
        "WindowsGetStringRawBuffer": (c_void_p, [c_void_p, POINTER(c_uint32)]),
    }
    lib = type("WinRt", (), {})()
    for name, (res, args) in sigs.items():
        fn = getattr(combase, name)
        fn.restype, fn.argtypes = res, args
        setattr(lib, name, fn)
    lib.ct, lib.GUID, lib.Rect = ctypes, GUID, Rect
    lib.iids = {}
    lib.uuid = uuid
    lib.local = threading.local()
    return lib


def _iid(lib, iid: str):
    g = lib.iids.get(iid)
    if g is None:
        g = lib.iids[iid] = lib.GUID.from_buffer_copy(lib.uuid.UUID(iid).bytes_le)
    return g


def _call(lib, obj, index: int, what: str, argtypes: tuple, *args):
    """The method at vtable slot `index` of `obj`, called with `args` typed by `argtypes`; OSError on a failed
    HRESULT."""
    if not obj:
        raise OSError(f"{what}: no object")
    ct = lib.ct
    vtbl = ct.cast(ct.c_void_p(obj), ct.POINTER(ct.POINTER(ct.c_void_p))).contents
    hr = ct.WINFUNCTYPE(ct.c_long, ct.c_void_p, *argtypes)(vtbl[index])(obj, *args)
    if hr < 0:
        raise OSError(f"{what}: HRESULT 0x{hr & 0xFFFFFFFF:08X}")


def _out_ptr(lib, obj, index: int, what: str, argtypes: tuple, *args) -> int | None:
    """A method whose last parameter receives an interface pointer (or HSTRING): that pointer, owned by the caller."""
    ct = lib.ct
    out = ct.c_void_p()
    _call(lib, obj, index, what, (*argtypes, ct.POINTER(ct.c_void_p)), *args, ct.byref(out))
    return out.value


def _get(lib, obj, index: int, what: str, ctype):
    """A getter of a plain value."""
    ct = lib.ct
    out = ctype()
    _call(lib, obj, index, what, (ct.POINTER(ctype),), ct.byref(out))
    return out.value


def _size(lib, vec) -> int:
    return _get(lib, vec, 7, "IVectorView.Size", lib.ct.c_uint32)


def _qi(lib, obj, iid: str, what: str) -> int:
    ct = lib.ct
    p = _out_ptr(lib, obj, 0, f"QueryInterface({what})", (ct.POINTER(lib.GUID),), ct.byref(_iid(lib, iid)))
    if not p:
        raise OSError(f"QueryInterface({what}) gave no pointer")
    return p


def _release(lib, obj):
    if obj:
        ct = lib.ct
        vtbl = ct.cast(ct.c_void_p(obj), ct.POINTER(ct.POINTER(ct.c_void_p))).contents
        ct.WINFUNCTYPE(ct.c_ulong, ct.c_void_p)(vtbl[2])(obj)  # IUnknown::Release


def _hstring(lib, s: str) -> int:
    """A new HSTRING for `s`; the caller deletes it."""
    ct = lib.ct
    out = ct.c_void_p()
    hr = lib.WindowsCreateString(s, len(s.encode("utf-16-le")) // 2, ct.byref(out))
    if hr < 0:
        raise OSError(f"WindowsCreateString: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    return out.value


def _string(lib, obj, index: int, what: str) -> str:
    """A string getter: the HSTRING it returns, read and deleted."""
    ct = lib.ct
    h = _out_ptr(lib, obj, index, what, ())
    if not h:
        return ""  # the empty HSTRING is null
    try:
        n = ct.c_uint32(0)
        p = lib.WindowsGetStringRawBuffer(h, ct.byref(n))
        return ct.wstring_at(p, n.value) if p and n.value else ""
    finally:
        lib.WindowsDeleteString(h)


def _factory(lib, name: str, iid: str) -> int:
    """The activation factory of runtime class `name`, as interface `iid`."""
    ct = lib.ct
    h = _hstring(lib, name)
    out = ct.c_void_p()
    try:
        hr = lib.RoGetActivationFactory(h, ct.byref(_iid(lib, iid)), ct.byref(out))
    finally:
        lib.WindowsDeleteString(h)
    if hr < 0 or not out.value:
        raise OSError(f"RoGetActivationFactory({name}): HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    return out.value


def _language(lib, tag: str) -> int:
    """A Windows.Globalization.Language for `tag` (ILanguageFactory.CreateLanguage); OSError for a tag it refuses."""
    ct = lib.ct
    factory = language = None
    try:
        factory = _factory(lib, "Windows.Globalization.Language", _IID_LANGUAGE_FACTORY)
        h = _hstring(lib, tag)
        try:
            language = _out_ptr(lib, factory, 6, "Language.CreateLanguage", (ct.c_void_p,), h)
        finally:
            lib.WindowsDeleteString(h)
        if not language:
            raise OSError(f"no language {tag!r}")
        return language
    finally:
        _release(lib, factory)


def _tag(lib, engine: int) -> str:
    """The BCP-47 tag of an engine's RecognizerLanguage."""
    language = _out_ptr(lib, engine, 7, "OcrEngine.RecognizerLanguage", ())
    try:
        if not language:
            raise OSError("Windows OCR engine has no recognizer language")
        return _string(lib, language, 6, "Language.LanguageTag")
    finally:
        _release(lib, language)


# ---- pixels and files ------------------------------------------------------------------------------------------------

def _fit(g: Gray, step: int) -> tuple[int, int, bytes]:
    """(w, h, data) of `g` keeping every step-th row and column."""
    if step <= 1:
        return g.w, g.h, g.data
    rows = range(0, g.h, step)
    data = b"".join(g.data[r * g.w:(r + 1) * g.w:step] for r in rows)
    return len(range(0, g.w, step)), len(rows), data


def _ocr_root() -> Path:
    return Path(os.environ.get("WINDIR") or os.environ.get("SystemRoot") or r"C:\Windows") / "OCR"


def _version(tag: str) -> str:
    """'<size>:<mtime_ns>/<size>:<mtime_ns>' of Windows.Media.Ocr.dll and of the recognizer's MsOcrRes.orp."""
    system = Path(os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows") / "System32"
    tag = tag.lower()
    folders = recognizers()
    folder = next((f for f in folders if f == tag), None) \
        or next((f for f in folders if f.startswith(tag + "-")), None) \
        or next((f for f in folders if f.split("-")[0] == tag.split("-")[0]), None)
    if folder is None:
        raise OSError(f"no recognizer folder for {tag!r} under {_ocr_root()}")
    parts = []
    for p in (system / "Windows.Media.Ocr.dll", _ocr_root() / folder / "MsOcrRes.orp"):
        st = p.stat()
        parts.append(f"{st.st_size}:{st.st_mtime_ns}")
    return "/".join(parts)
