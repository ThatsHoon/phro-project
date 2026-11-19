"""OCR results as the reader keeps them, whichever engine read the page."""
from dataclasses import dataclass


@dataclass
class Word:
    """One recognized word. The box is in pixels of the page's frame, y growing downward. `line` is the engine's
    line number; `conf` is the engine's confidence (0-100), None when the engine gives none."""
    text: str
    x: int
    y: int
    w: int
    h: int
    line: int
    conf: float | None


@dataclass
class OcrPage:
    """An engine's result for one bitmap. `w` and `h` are the frame the boxes are in, `angle` the text angle the
    engine reported, `turn` the rotation the reader applied (0, 90, 180 or 270), `engine` and `version` the ids
    that key caches."""
    w: int
    h: int
    words: list[Word]
    angle: float
    turn: int
    engine: str
    version: str
