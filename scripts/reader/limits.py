"""Budgets and the one error type every reader module raises."""

MAX_INPUT = 512 * 2**20
ZIP_TOTAL = 2**30
ZIP_RATIO = 200
ZIP_RATIO_FLOOR = 2**20  # the ratio is checked only for members over 1 MiB
MAX_DEPTH = 64
MAX_OUTPUT = 20 * 10**6
CONVERT_SECONDS = 45
SHEET_ROWS = 100_000
SHEET_COLS = 1_000        # columns one sheet row may reach
SHEET_CELLS = 1_000_000   # cells one document's sheets may materialize
KINDS = ("unsupported", "encrypted", "drm", "damaged", "too_large", "timeout")


class ReadError(Exception):
    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail
