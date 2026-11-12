"""Undo and redo: copies of the label after each change.

Changes in quick succession to the same thing (typing into one text,
dragging one slider) are one step: they share a key and come within
COALESCE_SECONDS of each other. The stack keeps at most LIMIT steps.
"""

from __future__ import annotations

import copy
import time
from collections.abc import Callable

from etiketto.model import Label

LIMIT = 200
COALESCE_SECONDS = 1.5


class History:
    def __init__(self, label: Label, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._undo: list[Label] = []
        self._redo: list[Label] = []
        self._current = copy.deepcopy(label)
        self._last: tuple[str | None, float] = (None, 0.0)

    def reset(self, label: Label) -> None:
        """Another label (opened, new): nothing to undo."""
        self.__init__(label, self._clock)  # type: ignore[misc]

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def record(self, label: Label, key: str | None = None) -> None:
        """The label after a change. key: what was changed (see above);
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
