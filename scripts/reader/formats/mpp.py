"""Microsoft Project (mpp, mpt): read only through MPXJ, in the mpp venv's child (`read.py convert <path> --engine
mpxj`, see mpp_mpxj). No mpp structure is parsed here: without the engine the file is `unsupported`.

The compound file is opened first, with or without the engine, for its `\\x01CompObj` only. One whose clipboard
format or ProgID does not start `MSProject.` is another format's compound file named .mpp, handed to `cfb_route` as
ppt's parse_compound does, so MPXJ never reads a renamed file. A zip named .mpp goes to `zip_route`.
"""
from ..containers.cfb import Compound, is_cfb
from ..limits import ReadError
from ..model import Doc
from . import cfb_route, zip_route

UNSUPPORTED = "Microsoft Project files are read only through MPXJ"
NOT_PROJECT = "no MSProject CompObj"


def parse(path, ctx, engine=None) -> Doc:
    with open(path, "rb") as f:
        head = f.read(8)
    if head[:2] == b"PK":  # a zip named .mpp: zip_route picks its parser, never this one
        return zip_route.parse(path, ctx)
    if not is_cfb(head):
        raise ReadError("damaged", "not a Microsoft Project file")
    other = route(path)
    if other is not None:  # another format's compound file named .mpp
        return cfb_route.parse(other, path, ctx)
    if engine == "mpxj":
        from . import mpp_mpxj
        return mpp_mpxj.read(path, ctx)
    raise ReadError("unsupported", UNSUPPORTED)


def route(src) -> str | None:
    """None when the compound file `src` (a path or its bytes) is a Project file by its CompObj; else the parser
    `cfb_route.route` names for it, or its ReadError (damaged "no MSProject CompObj" when nothing names one). A broken
    header, directory or FAT is Compound's own ReadError("damaged"); a CompObj whose chain breaks is not read
    (cfb_route catches it), so nothing else escapes."""
    with Compound(src) as cf:
        if cfb_route.msproject(cf):
            return None
        return cfb_route.route(cf, "mpp", NOT_PROJECT)
