"""The SVG path parser of the symbol library: every command, absolute and
relative, implicit repeats, arcs, number syntax and malformed data."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from etiketto.symbols.path import MAX_PATH_CHARS, PathError, arc_to_cubics, parse


def approx(segments):
    """Segments with rounded numbers, to compare against expected values."""
    return [(s[0], *(round(v, 6) + 0.0 for v in s[1:])) for s in segments]


def bezier(p0, c1, c2, p1, t):
    u = 1 - t
    return tuple(
        u**3 * a + 3 * u * u * t * b + 3 * u * t * t * c + t**3 * d
        for a, b, c, d in zip(p0, c1, c2, p1, strict=True)
    )


# --- lines, moves, close ----------------------------------------------------------


def test_moves_and_lines_absolute_and_relative():
    assert parse("M1,2 L3,4 l1,1 H10 h-2 V0 v5 Z") == [
        ("M", 1, 2),
        ("L", 3, 4),
        ("L", 4, 5),
        ("L", 10, 5),
        ("L", 8, 5),
        ("L", 8, 0),
        ("L", 8, 5),
        ("Z",),
    ]


def test_relative_move_and_close_return_to_the_subpath_start():
    # after z the current point is the start of the subpath: m is relative to it
    assert parse("m2,3 l4,0 z m1,1 l1,0z") == [
        ("M", 2, 3),
        ("L", 6, 3),
        ("Z",),
        ("M", 3, 4),
        ("L", 4, 4),
        ("Z",),
    ]


def test_implicit_repeats():
    # pairs after M are lines (after m relative ones); other commands repeat
    assert parse("M0 0 1 1 2 0") == [("M", 0, 0), ("L", 1, 1), ("L", 2, 0)]
    assert parse("m1 1 1 1 1 0") == [("M", 1, 1), ("L", 2, 2), ("L", 3, 2)]
    assert parse("M0,0 h1 2 v3 4") == [
        ("M", 0, 0),
        ("L", 1, 0),
        ("L", 3, 0),
        ("L", 3, 3),
        ("L", 3, 7),
    ]
    curves = parse("M0,0 c1,0 1,1 2,1 1,0 1,1 2,1")
    assert approx(curves) == [("M", 0, 0), ("C", 1, 0, 1, 1, 2, 1), ("C", 3, 1, 3, 2, 4, 2)]


def test_number_syntax():
    # no separators where the sign or a second dot starts the next number
    assert parse("M1.5.5L-1-2l+1e1,2E-1") == [
        ("M", 1.5, 0.5),
        ("L", -1, -2),
        ("L", 9, -1.8),
    ]
    assert parse("  M 3. ,\t.25\n") == [("M", 3, 0.25)]
    assert parse("") == [] and parse(" \n") == []


# --- curves -----------------------------------------------------------------------


def test_cubic_absolute_and_relative():
    assert parse("M1,1 C2,2 3,2 4,1") == [("M", 1, 1), ("C", 2, 2, 3, 2, 4, 1)]
    assert parse("M1,1 c1,1 2,1 3,0") == [("M", 1, 1), ("C", 2, 2, 3, 2, 4, 1)]


def test_smooth_cubic_mirrors_the_last_control_point():
    segments = parse("M0,0 C1,2 3,2 4,0 S7,-2 8,0 s3,-2 4,0")
    assert approx(segments[2:]) == [("C", 5, -2, 7, -2, 8, 0), ("C", 9, 2, 11, -2, 12, 0)]
    # without a curve before it the first control point is the current point
    assert parse("M1,1 S2,3 4,1")[1] == ("C", 1, 1, 2, 3, 4, 1)
    assert parse("M1,1 L2,2 s1,1 2,0")[2] == ("C", 2, 2, 3, 3, 4, 2)


def test_quadratic_becomes_the_same_cubic():
    (_, x1, y1, x2, y2, x, y) = parse("M0,0 Q3,6 6,0")[1]
    assert (x1, y1, x2, y2, x, y) == pytest.approx((2, 4, 4, 4, 6, 0))
    # the quadratic's middle point (t = 0.5) is (3, 3)
    assert bezier((0, 0), (x1, y1), (x2, y2), (x, y), 0.5) == pytest.approx((3, 3))
    assert approx(parse("M1,1 q3,6 6,0")[1:]) == [("C", 3, 5, 5, 5, 7, 1)]


def test_smooth_quadratic_mirrors_the_control_point():
    segments = parse("M0,0 Q3,6 6,0 T12,0 t6,0")
    # T mirrors (3,6) at (6,0): (9,-6); t then mirrors (9,-6) at (12,0): (15,6)
    assert approx(segments[2:]) == [
        ("C", 8, -4, 10, -4, 12, 0),
        ("C", 14, 4, 16, 4, 18, 0),
    ]
    # without a quadratic before it: a straight line as a curve
    assert approx(parse("M0,0 L3,0 T6,0")[2:]) == [("C", 3, 0, 4, 0, 6, 0)]
    # a cubic before T does not count
    assert approx(parse("M0,0 C1,1 2,1 3,0 T6,0")[2:]) == [("C", 3, 0, 4, 0, 6, 0)]


# --- arcs -------------------------------------------------------------------------


def _points_on(curves, start, samples=8):
    points, current = [], start
    for curve in curves:
        c1, c2, end = curve[0:2], curve[2:4], curve[4:6]
        points += [bezier(current, c1, c2, end, n / samples) for n in range(samples + 1)]
        current = end
    return points


@pytest.mark.parametrize(
    ("large", "sweep", "centre", "quarters"),
    [
        # sweep 1: clockwise on the screen (y down), the short way round (0,10)
        (0, 1, (0, 10), 1),
        (1, 0, (0, 10), 3),  # the long way round the same circle
        (0, 0, (10, 0), 1),
        (1, 1, (10, 0), 3),
    ],
)
def test_arc_flags_pick_one_of_four_arcs(large, sweep, centre, quarters):
    segments = parse("M0,0 A10,10 0 %d %d 10,10" % (large, sweep))
    curves = [s[1:] for s in segments[1:]]
    assert len(curves) == quarters  # at most a quarter circle per curve
    assert curves[-1][4:] == (10, 10)  # ends exactly on the end point
    for x, y in _points_on(curves, (0, 0)):
        assert math.hypot(x - centre[0], y - centre[1]) == pytest.approx(10, abs=0.03)


def test_compact_arc_flags_and_relative_arc():
    # flags need no separator: "001" is large=0, sweep=0, x=1
    compact = parse("M0,0a1 1 0 001 1")
    spaced = parse("M0,0 a1,1 0 0,0 1,1")
    assert compact == spaced
    assert compact[-1][-2:] == (1, 1)
    # relative arcs: only the end point moves, not the radii
    assert approx(parse("M5,5 a1,1 0 0 0 1,1")) == approx(
        [("M", 5, 5), *[("C", *c) for c in arc_to_cubics(5, 5, 1, 1, 0, False, False, 6, 6)]]
    )


def test_full_circle_from_two_arcs():
    curves = [s[1:] for s in parse("M2,12 A10,10 0 1,1 22,12 A10,10 0 1,1 2,12Z")[1:-1]]
    assert len(curves) == 4
    for x, y in _points_on(curves, (2, 12)):
        assert math.hypot(x - 12, y - 12) == pytest.approx(10, abs=0.03)


def test_arc_with_radii_too_small_is_scaled_up():
    # radius 1 cannot reach from (0,0) to (10,0): a half circle of radius 5
    curves = arc_to_cubics(0, 0, 1, 1, 0, False, True, 10, 0)
    assert len(curves) == 2
    for x, y in _points_on(curves, (0, 0)):
        assert math.hypot(x - 5, y) == pytest.approx(5, abs=0.02)
        assert y <= 1e-9  # sweep: through the upper half (y up is negative)


def test_rotated_elliptical_arc():
    # an ellipse 4 × 2 turned by 90°: tall, centre (0,0), from top to bottom
    curves = arc_to_cubics(0, -4, 4, 2, 90, False, True, 0, 4)
    for x, y in _points_on(curves, (0, -4)):
        assert (x / 2) ** 2 + (y / 4) ** 2 == pytest.approx(1, abs=0.01)
        assert x >= -1e-9  # sweep: through the right half


def test_degenerate_arcs():
    assert arc_to_cubics(1, 1, 5, 5, 0, False, False, 1, 1) == []  # nothing to draw
    assert arc_to_cubics(0, 0, 0, 5, 0, False, False, 3, 4) == [(0, 0, 3, 4, 3, 4)]  # a line
    # negative radii count as positive
    assert arc_to_cubics(0, 0, -10, -10, 0, False, True, 10, 10) == arc_to_cubics(
        0, 0, 10, 10, 0, False, True, 10, 10
    )


# --- malformed data ---------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        "L1,1",  # must start with a move
        "1,1",
        "M1",  # missing coordinate
        "M1,1 L",
        "M1,1 X2,2",  # unknown command
        "M1,1 Z 2,2",  # Z takes no numbers
        "M0,0 A1,1 0 2 0 1,1",  # flags are 0 or 1
        "M0,0 A1,1 0 0",
        "M1e999,0",  # not a finite number
        "M1,1 L2,2 <script>",
        "M--1,0",
    ],
)
def test_malformed_path_data_is_refused(data):
    with pytest.raises(PathError):
        parse(data)


@pytest.mark.parametrize(
    "data",
    [
        "M0 0a1e-9 1 0 0 0 1e-300 0",  # the arithmetic underflows
        "M0 0A1 1 0 0 0 1e-160 0",  # NaN on the way
        "M0 0A1e-8 1e-8 0 0 0 1e-300 1e-300",
        "M0 0A1000000 1e-9 45 1 1 1e-300 0",
    ],
)
def test_arcs_too_small_to_calculate_are_lines(data):
    move, line = parse(data)
    assert move == ("M", 0, 0) and line[0] == "C"
    assert line[3:5] == line[5:7]  # a straight line to the end point
    assert all(math.isfinite(v) for v in line[1:])


@pytest.mark.parametrize("separator", ["\f", "\v", "\u00a0", "\u2003", ";"])
def test_only_svg_separators(separator):
    # SVG white space is space, tab, CR and LF; the comma separates too
    assert parse("M1\t2\r\n3,4") == [("M", 1, 2), ("L", 3, 4)]
    with pytest.raises(PathError):
        parse("M1%s2" % separator)


def test_too_long_or_not_text_is_refused():
    with pytest.raises(PathError):
        parse("M0,0" + " L1,1" * (MAX_PATH_CHARS // 5))
    with pytest.raises(PathError):
        parse(None)  # type: ignore[arg-type]


@given(st.text(alphabet="MmLlHhVvCcSsQqTtAaZz0123456789.,-+eE \n", max_size=60))
def test_any_data_parses_or_raises_path_error(data):
    try:
        segments = parse(data)
    except PathError:
        return
    for segment in segments:
        assert segment[0] in ("M", "L", "C", "Z")
        assert all(math.isfinite(v) for v in segment[1:])


def test_arc_arithmetic_failures_fall_back_to_a_line(monkeypatch):
    from etiketto.symbols import path

    with pytest.raises(ArithmeticError):  # end points that coincide: no centre
        path._arc_center(1, 1, 1, 1, 0, False, False, 1, 1)
    line = [(0, 0, 3, 4, 3, 4)]
    for failure in (lambda *_a: 1 / 0, lambda *_a: math.ceil(math.nan)):
        monkeypatch.setattr(path, "_arc_curves", failure)
        assert arc_to_cubics(0, 0, 5, 5, 0, False, True, 3, 4) == line
    monkeypatch.setattr(path, "_arc_curves", lambda *_a: [(math.inf, 0, 0, 0, 3, 4)])
    assert arc_to_cubics(0, 0, 5, 5, 0, False, True, 3, 4) == line
