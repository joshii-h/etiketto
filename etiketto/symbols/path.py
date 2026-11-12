"""SVG path data ("M12,2A10,10 0 0,0 2,12Z") as a list of drawing commands.

A small parser for the `d` attribute of SVG paths (SVG 1.1, section 8.3),
enough to draw symbols with cairo without another dependency. The result
uses absolute coordinates and only four commands:

    ("M", x, y)                      start a new subpath
    ("L", x, y)                      line
    ("C", x1, y1, x2, y2, x, y)      cubic Bézier curve
    ("Z",)                           close the subpath

Horizontal and vertical lines become lines, quadratic curves and elliptical
arcs become cubic curves. The data comes from files (a label stores the path
of its symbols), so anything malformed raises PathError, and the length is
limited.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable

Segment = tuple
MAX_PATH_CHARS = 100_000  # the longest Material Design Icon has about 9000
# coordinates of a symbol lie within its 24 × 24 square; far larger ones are
# junk and would overflow in the arc calculation
MAX_COORDINATE = 1e6
MIN_RADIUS = 1e-9  # smaller arcs are lines

_NUMBER = re.compile(r"[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][-+]?[0-9]+)?")
_SEPARATOR = re.compile(r"[\s,]*")
# arguments per command; an arc has two flags among its seven
_ARITY = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}
_ARC_FLAGS = (3, 4)


class PathError(ValueError):
    """Path data that is not valid SVG path syntax."""


class _Scanner:
    """Reads commands, numbers and arc flags from path data."""

    def __init__(self, data: str):
        self.data = data
        self.pos = 0

    def skip(self) -> None:
        match = _SEPARATOR.match(self.data, self.pos)
        assert match  # the pattern also matches nothing
        self.pos = match.end()

    def at_end(self) -> bool:
        self.skip()
        return self.pos >= len(self.data)

    def command(self) -> str | None:
        """The next command letter, or None if a number follows."""
        self.skip()
        char = self.data[self.pos : self.pos + 1]
        if char and char.upper() in _ARITY:
            self.pos += 1
            return char
        return None

    def number(self) -> float:
        self.skip()
        match = _NUMBER.match(self.data, self.pos)
        if not match:
            raise PathError("number expected at %d" % self.pos)
        value = float(match.group())
        if not abs(value) <= MAX_COORDINATE:
            raise PathError("number out of range at %d" % self.pos)
        self.pos = match.end()
        return value

    def flag(self) -> float:
        """An arc flag: a single 0 or 1, also without a separator ("a1 1 0 001 1")."""
        self.skip()
        char = self.data[self.pos : self.pos + 1]
        if char not in ("0", "1"):
            raise PathError("arc flag expected at %d" % self.pos)
        self.pos += 1
        return float(char)

    def arguments(self, command: str) -> list[float]:
        upper = command.upper()
        if upper == "A":
            return [self.flag() if n in _ARC_FLAGS else self.number() for n in range(7)]
        return [self.number() for _ in range(_ARITY[upper])]


class _Builder:
    """Turns commands into absolute M, L, C and Z segments."""

    def __init__(self) -> None:
        self.segments: list[Segment] = []
        self.x = self.y = 0.0
        self.start = (0.0, 0.0)  # of the current subpath, for Z
        # the second control point of the last C/S and the control point of
        # the last Q/T: S and T mirror them
        self.cubic: tuple[float, float] | None = None
        self.quad: tuple[float, float] | None = None
        self._previous: tuple = (None, None)  # cubic and quad before this command
        self.handlers: dict[str, Callable[[list[float]], None]] = {
            "M": self._move,
            "L": self._line,
            "H": lambda a: self._line([a[0], self.y]),
            "V": lambda a: self._line([self.x, a[0]]),
            "C": self._cubic,
            "S": self._smooth_cubic,
            "Q": self._quadratic,
            "T": self._smooth_quadratic,
            "A": self._arc,
            "Z": self._close,
        }

    def apply(self, command: str, args: list[float]) -> None:
        upper = command.upper()
        if command != upper:  # relative: to the current point
            args = self._absolute(upper, args)
        cubic, quad = self.cubic, self.quad
        self.cubic = self.quad = None
        self._previous = cubic, quad
        self.handlers[upper](args)

    def _absolute(self, upper: str, args: list[float]) -> list[float]:
        if upper == "H":
            return [args[0] + self.x]
        if upper == "V":
            return [args[0] + self.y]
        if upper == "A":  # only the end point moves
            return [*args[:5], args[5] + self.x, args[6] + self.y]
        return [value + (self.y if n % 2 else self.x) for n, value in enumerate(args)]

    def _to(self, x: float, y: float) -> None:
        self.x, self.y = x, y

    def _move(self, a: list[float]) -> None:
        self.segments.append(("M", a[0], a[1]))
        self.start = (a[0], a[1])
        self._to(a[0], a[1])

    def _line(self, a: list[float]) -> None:
        self.segments.append(("L", a[0], a[1]))
        self._to(a[0], a[1])

    def _cubic(self, a: list[float]) -> None:
        self.segments.append(("C", *a))
        self.cubic = (a[2], a[3])
        self._to(a[4], a[5])

    def _smooth_cubic(self, a: list[float]) -> None:
        previous = self._previous[0]
        x1, y1 = (
            (2 * self.x - previous[0], 2 * self.y - previous[1]) if previous else (self.x, self.y)
        )
        self._cubic([x1, y1, *a])

    def _quadratic(self, a: list[float]) -> None:
        qx, qy, x, y = a
        # the cubic curve with the same shape: control points 2/3 of the way
        # from the end points to the quadratic control point
        self.segments.append(
            (
                "C",
                self.x + 2 / 3 * (qx - self.x),
                self.y + 2 / 3 * (qy - self.y),
                x + 2 / 3 * (qx - x),
                y + 2 / 3 * (qy - y),
                x,
                y,
            )
        )
        self.quad = (qx, qy)
        self._to(x, y)

    def _smooth_quadratic(self, a: list[float]) -> None:
        previous = self._previous[1]
        qx, qy = (
            (2 * self.x - previous[0], 2 * self.y - previous[1]) if previous else (self.x, self.y)
        )
        self._quadratic([qx, qy, *a])

    def _arc(self, a: list[float]) -> None:
        rx, ry, angle, large, sweep, x, y = a
        for curve in arc_to_cubics(self.x, self.y, rx, ry, angle, bool(large), bool(sweep), x, y):
            self.segments.append(("C", *curve))
        self._to(x, y)

    def _close(self, _a: list[float]) -> None:
        self.segments.append(("Z",))
        self._to(*self.start)


def _next_command(scanner: _Scanner, last: str | None) -> str:
    """The command whose arguments follow: a new one, or the last again."""
    letter = scanner.command()
    if letter is None:  # more arguments: the last command repeats
        if last is None or last in "Zz":
            raise PathError("command expected at %d" % scanner.pos)
        return last
    if last is None and letter not in "Mm":
        raise PathError("path data must start with M")
    return letter


def parse(data: str) -> list[Segment]:
    """The segments of SVG path data; PathError if it is not valid."""
    if not isinstance(data, str) or len(data) > MAX_PATH_CHARS:
        raise PathError("path data too long or not text")
    scanner, builder = _Scanner(data), _Builder()
    command = None
    while not scanner.at_end():
        command = _next_command(scanner, command)
        builder.apply(command, scanner.arguments(command))
        if command in "Mm":  # pairs after a move are lines
            command = "L" if command == "M" else "l"
    return builder.segments


# --- elliptical arcs --------------------------------------------------------


def _arc_center(x1, y1, rx, ry, phi, large, sweep, x2, y2):
    """Centre, radii, start angle and sweep of an arc given by its end points
    (SVG 1.1, appendix F.6.5 and F.6.6)."""
    cos, sin = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    px, py = cos * dx + sin * dy, -sin * dx + cos * dy
    scale = (px / rx) ** 2 + (py / ry) ** 2
    if scale > 1:  # radii too small for the end points: enlarge them
        rx, ry = rx * math.sqrt(scale), ry * math.sqrt(scale)
    num = rx * rx * ry * ry - rx * rx * py * py - ry * ry * px * px
    den = rx * rx * py * py + ry * ry * px * px
    root = math.sqrt(max(0.0, num / den)) * (-1 if large == sweep else 1)
    cxp, cyp = root * rx * py / ry, -root * ry * px / rx
    cx = cos * cxp - sin * cyp + (x1 + x2) / 2
    cy = sin * cxp + cos * cyp + (y1 + y2) / 2
    start = math.atan2((py - cyp) / ry, (px - cxp) / rx)
    end = math.atan2((-py - cyp) / ry, (-px - cxp) / rx)
    delta = end - start
    if sweep and delta < 0:
        delta += 2 * math.pi
    elif not sweep and delta > 0:
        delta -= 2 * math.pi
    return cx, cy, rx, ry, start, delta


def arc_to_cubics(x1, y1, rx, ry, angle, large, sweep, x2, y2) -> list[tuple]:
    """An SVG arc from (x1, y1) to (x2, y2) as cubic curves (x1', y1', x2',
    y2', x, y), each at most a quarter of the ellipse; a line if a radius is
    zero, nothing if the end points are the same."""
    if (x1, y1) == (x2, y2):
        return []
    rx, ry = abs(rx), abs(ry)
    if rx < MIN_RADIUS or ry < MIN_RADIUS:
        return [(x1, y1, x2, y2, x2, y2)]
    phi = math.radians(angle % 360)
    cx, cy, rx, ry, start, delta = _arc_center(x1, y1, rx, ry, phi, large, sweep, x2, y2)
    count = max(1, math.ceil(abs(delta) / (math.pi / 2) - 1e-9))
    step = delta / count
    k = 4 / 3 * math.tan(step / 4)  # control point distance for a unit circle
    cos, sin = math.cos(phi), math.sin(phi)

    def point(t: float, dx: float = 0.0, dy: float = 0.0) -> tuple[float, float]:
        ex, ey = rx * (math.cos(t) + dx), ry * (math.sin(t) + dy)
        return cx + cos * ex - sin * ey, cy + sin * ex + cos * ey

    curves = []
    for n in range(count):
        t1, t2 = start + n * step, start + (n + 1) * step
        c1 = point(t1, -k * math.sin(t1), k * math.cos(t1))
        c2 = point(t2, k * math.sin(t2), -k * math.cos(t2))
        end = (x2, y2) if n == count - 1 else point(t2)  # exactly on the end point
        curves.append((*c1, *c2, *end))
    return curves
