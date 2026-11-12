"""Undo and redo: copies of the label after each change.

Typing into one text or changing one number in quick succession is one
step: such changes share a key (typing_key()) and come within
COALESCE_SECONDS of each other; a switch or a choice is always a step of
its own. The stack keeps at most LIMIT steps and about LIMIT_BYTES of
embedded files (an image replaced again and again).
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable, Iterator

from etiketto.model import Label

LIMIT = 200
LIMIT_BYTES = 256 * 2**20
COALESCE_SECONDS = 1.5
TYPED = frozenset({"text", "data", "name"})  # fields typed into
# fields set from spin buttons (a choice of the same type, like the tape
# width, is not among them)
SPUN = frozenset(
    {
        "threshold",
        "size_pt",
        "size_mm",
        "letter_spacing",
        "line_spacing",
        "min_size_pt",
        "max_width_mm",
        "padding_mm",
        "corner_mm",
        "frame_mm",
        "outline_mm",
        "shadow_mm",
        "brightness",
        "contrast",
        "gamma",
        "border_mm",
        "width_mm",
        "height_mm",
        "line_mm",
        "gap_mm",
        "margin_mm",
        "edge_mm",
        "length_mm",
        "border_radius_mm",
        "border_inset_mm",
    }
)


def typing_key(prefix: str, changes: dict) -> str | None:
    """The key that joins a change with the one before: typing into a text
    field or turning a spin button (by the field, SPUN); None (a step of
    its own) for switches, choices of any type (the tape width too) and
    colours."""
    spun = set(changes) <= SPUN and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in changes.values()
    )
    if not changes or not (spun or set(changes) <= TYPED):
        return None
    return prefix + ":" + ",".join(sorted(changes))


def _files(label: Label) -> Iterator[str]:
    """The embedded files of a label (base64 text, shared between copies)."""
    for element in label.elements:
        data = getattr(element, "data", None)
        if isinstance(data, str) and getattr(element, "kind", "") in ("image", "page"):
            yield data
    if label.background_image:
        yield label.background_image.data


class History:
    def __init__(self, label: Label, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._undo: list[Label] = []
        self._redo: list[Label] = []
        self._current = copy.deepcopy(label)
        self._saved: Label | None = self._current
        self._last: tuple[str | None, float] = (None, 0.0)

    def reset(self, label: Label) -> None:
        """Another label (opened, new): nothing to undo, as saved."""
        self.__init__(label, self._clock)  # type: ignore[misc]

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    @property
    def at_saved(self) -> bool:
        """Whether the label is as it was opened or last saved."""
        return self._current is self._saved

    def mark_saved(self) -> None:
        """The label as it is now was saved; the next change is a new step."""
        self._saved = self._current
        self.end_step()

    def end_step(self) -> None:
        """The next change is a step of its own (after a switch, saving)."""
        self._last = (None, 0.0)

    def record(self, label: Label, key: str | None = None) -> None:
        """The label after a change. key: what was changed (typing_key());
        None never joins the step before."""
        now = self._clock()
        last_key, last_time = self._last
        joined = key is not None and key == last_key and now - last_time <= COALESCE_SECONDS
        if not joined:
            self._undo.append(self._current)
            del self._undo[:-LIMIT]
        self._current = copy.deepcopy(label)
        self._redo.clear()
        self._last = (key, now)
        self._limit_bytes()

    def _limit_bytes(self) -> None:
        """Drop the oldest steps while their embedded files take too much."""
        while self._undo and self._bytes() > LIMIT_BYTES:
            del self._undo[0]

    def _bytes(self) -> int:
        seen: dict[int, int] = {}
        for label in (*self._undo, *self._redo, self._current):
            for data in _files(label):
                seen[id(data)] = len(data)
        return sum(seen.values())

    def undo(self) -> Label | None:
        """The label before the last step (a copy to use), None if none."""
        if not self._undo:
            return None
        self._redo.append(self._current)
        self._current = self._undo.pop()
        self._last = (None, 0.0)
        return copy.deepcopy(self._current)

    def redo(self) -> Label | None:
        if not self._redo:
            return None
        self._undo.append(self._current)
        self._current = self._redo.pop()
        self._last = (None, 0.0)
        return copy.deepcopy(self._current)

    def patch(self, name: str, value) -> None:
        """Set a field in every step: what printing moved on (the counter)
        is not taken back by undo."""
        for label in (*self._undo, *self._redo, self._current):
            setattr(label, name, value)
