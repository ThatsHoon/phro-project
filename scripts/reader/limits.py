"""Budgets and the one error type every reader module raises."""

MAX_INPUT = 512 * 2**20
ZIP_TOTAL = 2**30
ZIP_RATIO = 200
ZIP_RATIO_FLOOR = 2**20  # the ratio is checked only for members over 1 MiB
MAX_DEPTH = 64
MAX_OUTPUT = 20 * 10**6
CONVERT_SECONDS = 45
HOOK_SECONDS = 55     # the gate's whole budget, hashing included, under the 60-second hook timeout in hooks.json
HOOK_CHILD_MIN = 5    # with less than this left the gate denies instead of starting a conversion
SHEET_ROWS = 100_000
SHEET_COLS = 1_000        # columns one sheet row may reach
SHEET_CELLS = 500_000     # cells one document's sheets may materialize
SHEET_CHARS = 20_000_000  # characters of cell text, copies counted, one document's sheets may materialize
GRID_COLS = 1_000        # columns a span may reach in a rendered table
GRID_SLOTS = 2_000_000    # grid slots all spans of one rendered table may cover
CHART_CELLS = 100_000     # cells one chart table may hold, header row included (series x rows)
CFB_STREAM = 256 * 2**20  # bytes of one compound-file stream
CFB_SECTORS = 1_000_000
CFB_ENTRIES = 131_072     # directory entries
CFB_DEPTH = 128           # storage nesting
CFB_CHAIN = 4_000_000     # steps of one sector chain
HWP_RECORDS = 1_000_000   # records of one HWP stream
KINDS = ("unsupported", "encrypted", "drm", "damaged", "too_large", "timeout")
# a too_large detail starts with one of these and a colon, so the gate can name the limit that was hit
BIG_INPUT = "input"  # the file is over MAX_INPUT
BIG_PART = "part"    # one zip member passes ZIP_RATIO
BIG_TOTAL = "total"  # the members read so far pass ZIP_TOTAL


class ReadError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
