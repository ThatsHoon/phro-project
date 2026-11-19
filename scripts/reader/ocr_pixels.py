"""The pixels OCR reads: Gray8 bitmaps the reader decoded itself."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Gray:
    """A Gray8 bitmap, row-major with no padding: 0 is black, 255 is white."""
    w: int
    h: int
    data: bytes

    def __post_init__(self):
        if self.w <= 0 or self.h <= 0 or len(self.data) != self.w * self.h:
            raise ValueError(f"Gray {self.w}x{self.h} with {len(self.data)} bytes")
