"""VBA project source ([MS-OVBA]): the modules of a project storage, decompressed and decoded, never run.

A project is a storage holding `VBA/dir` and one stream a module: Office packages keep it as the compound file
vbaProject.bin, Word's .doc under `Macros/`, Excel's .xls and Visio's .vsd under `_VBA_PROJECT_CUR/`, Publisher at
the root. A reader names the storage of its own format (`compound`), so the project of an object the file embeds
is never taken for the document's. `dir` is a compressed container (2.4.1) of records `id u16, size u32, data`
(PROJECTVERSION 0x0009 alone holds two more bytes than its size says): PROJECTCODEPAGE 0x0003 gives the code page
of the names and the source, PROJECTMODULES 0x000F how many modules follow; a module is MODULENAME 0x0019 (its
Unicode form 0x0047 wins), MODULESTREAMNAME 0x001A (Unicode 0x0032), MODULEOFFSET 0x0031 (where the source's
compressed container starts in the stream, after the compiled code), MODULETYPE 0x0021 (a procedural module) or
0x0022 (a document, class or form module), and ends at 0x002B.

`modules` gives (name, kind, source) for each module that holds code: its `Attribute` lines are left out, all but a
procedure's VB_Description, and a module with no other line is not listed. One document reads at most VBA_MODULES
modules and decompresses at most VBA_BYTES bytes, its dir streams and a container that fails part way included, all
its projects together. A module past either, one whose stream is over EMBED_BYTES or does not decompress, and one
the dir leaves without its end record is counted under vba_module. A project whose `dir` cannot be read or gives
none of the modules it announces, and one met after the budget is spent, is named under vba, as a project never
read was. `compound` hands a compound file's own project to `Context.vba` and `package` reads the projects of a
zip package; reader.parse puts `Context.vba` in the Doc's meta for markdown to list and write under assets/vba.
"""
import codecs
import re
import struct

from .. import limits
from ..limits import ReadError
from .cfb import CfbError, Compound, is_cfb

_ATTRIBUTE = re.compile(r"Attribute (VB_|\S+\.VB_(?!Description))")
_KINDS = {0x0021: "module", 0x0022: "class or document"}
_MAC = {10000: "mac_roman", 10006: "mac_greek", 10007: "mac_cyrillic", 10029: "mac_latin2", 10079: "mac_iceland",
        10081: "mac_turkish"}
_UNREAD = (ValueError, KeyError, CfbError, struct.error)


def decompress(data: bytes, cap: int) -> bytes:
    """The bytes of the compressed container `data` (2.4.1.3.1). ValueError, its second argument the bytes made so
    far, when it is not one, when a copy reaches before its chunk, or when it would give more than `cap` bytes."""
    if not data or data[0] != 1:
        raise ValueError("not a compressed container", 0)
    out = bytearray()
    at, n = 1, len(data)
    while at + 2 <= n:
        head = data[at] | data[at + 1] << 8
        if head >> 12 & 7 != 3:
            raise ValueError("chunk signature", len(out))
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
                        raise ValueError("copy token", len(out))
                    token = data[at] | data[at + 1] << 8
                    at += 2
                    done = len(out) - start
                    bits = max((done - 1).bit_length(), 4)
                    length = (token & 0xFFFF >> bits) + 3
                    offset = (token >> 16 - bits) + 1
                    if offset > done:
                        raise ValueError("copy offset", len(out))
                    if offset >= length:
                        out += out[-offset:len(out) - offset + length]
                    else:
                        for _ in range(length):
                            out.append(out[-offset])
            at = end
        if len(out) > cap:
            raise ValueError("too large", len(out))
    return bytes(out)


def _spent(ctx) -> bool:
    return ctx.vba_bytes >= limits.VBA_BYTES


def _unpack(cf, name: str, offset: int, ctx) -> bytes:
    """Stream `name` of `cf` from `offset` on, decompressed; what it made is charged to the document's VBA_BYTES
    whether or not it ends well. ValueError for a stream over EMBED_BYTES."""
    if cf.size(name) > limits.EMBED_BYTES:
        raise ValueError("stream too large", 0)
    try:
        out = decompress(cf.read(name)[offset:], limits.VBA_BYTES - ctx.vba_bytes)
    except ValueError as e:
        ctx.vba_bytes += e.args[1]
        raise
    ctx.vba_bytes += len(out)
    return out


def _codec(page: int) -> str:
    try:
        return _MAC.get(page) or codecs.lookup(f"cp{page}").name
    except LookupError:
        return "cp1252"


def _entries(data: bytes):
    """(codec, [(name, stream name, offset, kind)], modules left without their end record, modules announced) of a
    decompressed dir stream."""
    codec, mods, cur, lost, told = "cp1252", [], None, 0, 0
    at, n = 0, len(data)
    while at + 6 <= n:
        rid, size = struct.unpack_from("<HI", data, at)
        body = data[at + 6:at + 6 + size]
        at += 6 + size + (2 if rid == 0x0009 else 0)
        if rid == 0x0003 and size >= 2:
            codec = _codec(struct.unpack_from("<H", body)[0])
        elif rid == 0x000F and size >= 2:
            told = struct.unpack_from("<H", body)[0]
        elif rid == 0x0019:
            lost += cur is not None
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
    return codec, mods, lost + (cur is not None), told


def _source(text: str) -> str:
    lines = [ln for ln in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
             if not _ATTRIBUTE.match(ln)]
    while lines and not lines[-1].strip():
        lines.pop()
    while lines and not lines[0].strip():
        lines.pop(0)
    return "\n".join(lines)


def compound(cf, ctx, *prefixes):
    """The modules of compound file `cf`'s own project go to ctx.vba: the project under the first of `prefixes`
    (storage paths that end in "/", "" for the root) that holds a `VBA/dir`. When none does, a file that holds
    one of those storages (for the root, a `VBA` storage) is named under vba."""
    for prefix in prefixes:
        got = modules(cf, prefix, ctx)
        if got is not None:
            ctx.vba += got
            return
    held = tuple((p or "VBA/").upper() for p in prefixes)
    if any(n.upper().startswith(held) for n in cf.names()):
        ctx.skipped["vba"] = 1


def package(pkg, ctx) -> list:
    """The modules of every vbaProject.bin of zip package `pkg`. One that is no compound file, is over EMBED_BYTES,
    holds no project or comes after the budget is spent is named under vba."""
    out = []
    for name in pkg.names:
        if name.rsplit("/", 1)[-1].lower() != "vbaproject.bin":
            continue
        got = None
        try:
            if not _spent(ctx) and pkg.size(name) <= limits.EMBED_BYTES:
                data = pkg.read(name)
                if is_cfb(data[:8]):
                    with Compound(data) as cf:
                        got = modules(cf, "", ctx)
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


def modules(cf, prefix: str, ctx) -> list | None:
    """[(name, kind, source)] of the project under `prefix` in storage `cf` (`exists`, `size` and `read` by stream
    name, as containers.cfb.Compound has), as the module names it; None when there is no project."""
    name = prefix + "VBA/dir"
    if not cf.exists(name):
        return None
    if _spent(ctx):
        ctx.skipped["vba"] = 1
        return []
    try:
        codec, entries, lost, told = _entries(_unpack(cf, name, 0, ctx))
    except _UNREAD:
        ctx.skipped["vba"] = 1
        return []
    except ReadError as e:
        if e.kind not in ("damaged", "too_large"):
            raise
        ctx.skipped["vba"] = 1
        return []
    if lost:
        ctx.skipped["vba_module"] += lost
    elif told and not entries:
        ctx.skipped["vba"] = 1
    out = []
    for k, m in enumerate(entries):
        if ctx.vba_modules >= limits.VBA_MODULES or _spent(ctx):
            ctx.skipped["vba_module"] += len(entries) - k
            break
        ctx.vba_modules += 1
        stream = prefix + "VBA/" + (m["stream"] or m["name"])
        try:
            if m["offset"] is None or not cf.exists(stream):
                raise ValueError("no stream")
            raw = _unpack(cf, stream, m["offset"], ctx)
        except _UNREAD:
            ctx.skipped["vba_module"] += 1
            continue
        except ReadError as e:
            if e.kind not in ("damaged", "too_large"):
                raise
            ctx.skipped["vba_module"] += 1
            continue
        text = _source(raw.decode(codec, "replace"))
        if text:
            out.append((m["name"], m["kind"], text))
    return out
