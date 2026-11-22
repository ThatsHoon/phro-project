"""VBA project source ([MS-OVBA]): the modules of a project storage, decompressed and decoded, never run.

A project is a storage holding `VBA/dir` and one stream a module: Office packages keep it as the compound file
vbaProject.bin, Word's .doc under `Macros/`, Excel's .xls and Visio's .vsd under `_VBA_PROJECT_CUR/`, Publisher at
the root. `dir` is a compressed container (2.4.1) of records `id u16, size u32, data` (PROJECTVERSION 0x0009 alone
holds two more bytes than its size says): PROJECTCODEPAGE 0x0003 gives the code page of the names and the source;
a module is MODULENAME 0x0019 (its Unicode form 0x0047 wins), MODULESTREAMNAME 0x001A (Unicode 0x0032),
MODULEOFFSET 0x0031 (where the source's compressed container starts in the stream, after the compiled code),
MODULETYPE 0x0021 (a procedural module) or 0x0022 (a document, class or form module), and ends at 0x002B.

`modules` gives (name, kind, source) for each module that holds code: its `Attribute` lines are left out, all but a
procedure's VB_Description, and a module with no other line is not listed. At most VBA_MODULES modules of a
project and VBA_BYTES bytes of source in a document are read; a module past either, or one whose stream does not
decompress, is counted under vba_module. A project whose `dir` cannot be read is named under vba, as a project
never read was. `project` finds the first project of a compound file, `compound` hands it to `Context.vba` and
`package` reads the projects of a zip package; reader.parse puts `Context.vba` in the Doc's meta for markdown to
list and write under assets/vba.
"""
import codecs
import re
import struct

from .. import limits
from ..limits import ReadError
from .cfb import CfbError, Compound, is_cfb

_DIR_BYTES = 2**22  # bytes a dir stream may decompress to
_ATTRIBUTE = re.compile(r"Attribute (VB_|\S+\.VB_(?!Description))")
_KINDS = {0x0021: "module", 0x0022: "class or document"}


def decompress(data: bytes, cap: int) -> bytes:
    """The bytes of the compressed container `data` (2.4.1.3.1). ValueError when it is not one, when a copy
    reaches before its chunk, or when it would give more than `cap` bytes."""
    if not data or data[0] != 1:
        raise ValueError("not a compressed container")
    out = bytearray()
    at, n = 1, len(data)
    while at + 2 <= n:
        head = data[at] | data[at + 1] << 8
        if head >> 12 & 7 != 3:
            raise ValueError("chunk signature")
        end = min(at + (head & 0xFFF) + 3, n)
        at += 2
        if not head & 0x8000:
            out += data[at:at + 4096]
            at += 4096
        else:
            start = len(out)
            while at < end:
                flags = data[at]
                at += 1
                for bit in range(8):
                    if at >= end:
                        break
                    if not flags >> bit & 1:
                        out.append(data[at])
                        at += 1
                        continue
                    if at + 2 > end:
                        raise ValueError("copy token")
                    token = data[at] | data[at + 1] << 8
                    at += 2
                    done = len(out) - start
                    bits = max((done - 1).bit_length(), 4)
                    length = (token & 0xFFFF >> bits) + 3
                    offset = (token >> 16 - bits) + 1
                    if offset > done:
                        raise ValueError("copy offset")
                    if offset >= length:
                        out += out[-offset:len(out) - offset + length]
                    else:
                        for _ in range(length):
                            out.append(out[-offset])
            at = end
        if len(out) > cap:
            raise ValueError("too large")
    return bytes(out)


def _codec(page: int) -> str:
    try:
        return codecs.lookup(f"cp{page}").name
    except LookupError:
        return "cp1252"


def _entries(data: bytes):
    """(codec, [(name, stream name, offset, kind)]) of a decompressed dir stream."""
    codec, mods, cur = "cp1252", [], None
    at, n = 0, len(data)
    while at + 6 <= n:
        rid, size = struct.unpack_from("<HI", data, at)
        body = data[at + 6:at + 6 + size]
        at += 6 + size + (2 if rid == 0x0009 else 0)
        if rid == 0x0003 and size >= 2:
            codec = _codec(struct.unpack_from("<H", body)[0])
        elif rid == 0x0019:
            cur = {"name": body.decode(codec, "replace"), "stream": None, "offset": None, "kind": "module"}
        elif cur is None:
            continue
        elif rid == 0x0047:
            cur["name"] = body.decode("utf-16-le", "replace")
        elif rid == 0x001A:
            cur["stream"] = body.decode(codec, "replace")
        elif rid == 0x0032:
            cur["stream"] = body.decode("utf-16-le", "replace")
        elif rid == 0x0031 and size >= 4:
            cur["offset"] = struct.unpack_from("<I", body)[0]
        elif rid in _KINDS:
            cur["kind"] = _KINDS[rid]
        elif rid == 0x002B:
            mods.append(cur)
            cur = None
    return codec, mods


def _source(text: str) -> str:
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
             if not _ATTRIBUTE.match(ln)]
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def project(cf, ctx) -> list | None:
    """The modules of the first project in compound file `cf` (the storage whose `VBA/dir` comes first in
    `cf.names()`), None when it holds no `VBA/dir`."""
    for name in cf.names():
        upper = name.upper()
        if upper == "VBA/DIR" or upper.endswith("/VBA/DIR"):
            return modules(cf, name[:-7], ctx)
    return None


def compound(cf, ctx, present: bool):
    """The modules of compound file `cf`'s VBA project go to ctx.vba; a project storage that is `present` with no
    `VBA/dir` to read is named under vba."""
    got = project(cf, ctx)
    if got is None:
        if present:
            ctx.skipped["vba"] = 1
    else:
        ctx.vba += got


def package(pkg, ctx) -> list:
    """The modules of every vbaProject.bin of zip package `pkg`. One that is no compound file, is over EMBED_BYTES
    or holds no project is named under vba."""
    out = []
    for name in pkg.names:
        if name.rsplit("/", 1)[-1].lower() != "vbaproject.bin":
            continue
        got = None
        try:
            if pkg.size(name) <= limits.EMBED_BYTES:
                data = pkg.read(name)
                if is_cfb(data[:8]):
                    with Compound(data) as cf:
                        got = project(cf, ctx)
        except (CfbError, KeyError):
            pass
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
        if got is None:
            ctx.skipped["vba"] = 1
        else:
            out += got
    return out


def modules(cf, prefix: str, ctx) -> list:
    """[(name, kind, source)] of the project under `prefix` in storage `cf` (`exists`, `size` and `read` by stream
    name, as containers.cfb.Compound has), as the module names it; [] when there is no project."""
    name = prefix + "VBA/dir"
    if not cf.exists(name):
        return []
    try:
        codec, entries = _entries(decompress(cf.read(name), _DIR_BYTES))
    except (ValueError, KeyError, CfbError, struct.error):
        ctx.skipped["vba"] = 1
        return []
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        ctx.skipped["vba"] = 1
        return []
    out = []
    for k, m in enumerate(entries):
        room = limits.VBA_BYTES - ctx.vba_bytes
        if k >= limits.VBA_MODULES or room <= 0:
            ctx.skipped["vba_module"] += len(entries) - k
            break
        stream = prefix + "VBA/" + (m["stream"] or m["name"])
        try:
            if m["offset"] is None or not cf.exists(stream):
                raise ValueError("no stream")
            raw = decompress(cf.read(stream)[m["offset"]:], room)
        except (ValueError, KeyError, CfbError):
            ctx.skipped["vba_module"] += 1
            continue
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            ctx.skipped["vba_module"] += 1
            continue
        ctx.vba_bytes += len(raw)
        text = _source(raw.decode(codec, "replace"))
        if text:
            out.append((m["name"], m["kind"], text))
    return out
