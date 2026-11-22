"""What the slide readers share: the reading order and its document-wide budget, repeated content keyed once.

Run: python -I -X utf8 tests/test_reader_deck.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from reader import limits  # noqa: E402
from reader.formats import deck  # noqa: E402
from reader.model import Para, Text  # noqa: E402

COLS = [(0, 0, 4, 2), (0, 3, 4, 5), (10, 0, 14, 2), (10, 3, 14, 5)]


def test_columns_read_down_then_across():
    assert deck.Order()(COLS) == [0, 1, 2, 3]


def test_shape_without_a_box_keeps_its_place():
    assert deck.Order()([None, COLS[2], None, COLS[0]]) == [0, 2, 3, 1]


def test_budget_is_one_for_the_document():
    order = deck.Order()
    assert order.left == limits.XYCUT_WORK
    order.left = 8
    assert order(COLS) == [0, 1, 2, 3] and order.left == 0
    assert order(COLS) == [0, 2, 1, 3]


def test_many_crowded_slides_stay_bounded():
    order = deck.Order()
    stair = [(k, k, k + 0.5, k + 0.5) for k in range(2000)]
    t = time.perf_counter()
    for _ in range(8):
        assert order(stair) == list(range(2000))
    assert time.perf_counter() - t < 6, time.perf_counter() - t


def test_overlapping_column_without_a_row_cut_reads_top_down():
    body = (838200, 1825625, 8610600, 6176963)
    box = (9071332, 2464641, 11929589, 2833973)
    table = (1765552, 3868593, 9893552, 5250353)
    assert deck.Order()([body, box, table]) == [0, 1, 2]


def test_repeated_keys_an_item_once():
    blocks, seen, real = [Para([Text("logo")])], [], deck._key

    def counted(v):
        if v is blocks:
            seen.append(1)
        return real(v)

    deck._key = counted
    try:
        rep = deck.Repeated()
        for n in range(1, 400):
            rep.add(n, blocks)
        rep.add(7, [Para([Text("logo")])])
    finally:
        deck._key = real
    assert len(seen) == 1, len(seen)
    assert len(rep.items) == 1 and len(next(iter(rep.items.values()))[1]) == 399


def test_two_columns_of_uneven_rows_that_overlap_a_little_read_by_column():
    assert deck.Order()([(0, 0, 520, 150), (0, 160, 520, 400), (480, 0, 1000, 250), (480, 260, 1000, 400)]) == [0, 1, 2, 3]
    assert deck.Order()([(0, 0, 520, 300), (0, 310, 520, 400), (480, 0, 1000, 80), (480, 90, 1000, 400)]) == [0, 1, 2, 3]


def test_side_by_side_boxes_that_overlap_a_little_read_left_first():
    pair = [(0, 100, 520, 400), (480, 90, 1000, 400)]
    assert deck.Order()(pair) == [0, 1], deck.Order()(pair)
    assert deck.Order()([(0, 0, 1000, 60)] + pair) == [0, 1, 2]
    stacked = [(300, 300, 1000, 400), (0, 100, 520, 200)]
    assert deck.Order()(stacked) == [1, 0], deck.Order()(stacked)


def main():
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("pass")


if __name__ == "__main__":
    main()
