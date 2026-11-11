"""A print job: variables filled in per label, rendered and sent to a backend.

Used by the window and by the command line. Without variables that change
from label to label the label is rendered once and the printer makes the
copies; with them (counter, copy number, UUID, ID) every copy is rendered
and sent on its own, so five copies are five numbers.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import datetime

from PIL import Image

from etiketto.backends.base import Backend, PrintOptions, PrintResult, Progress, ProgressCallback
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, RenderWarning, render
from etiketto.variables import Values, expand_label, uses_counter, varies_per_copy


class PrepareFailed(Exception):
    """The label could not be rendered for printing; the cause is chained."""


def merge_results(results: list[PrintResult | None]) -> PrintResult | None:
    """One result for the copies sent one by one."""
    queued = any(r.queued for r in results if r)
    mode = next((r.mode for r in results if r and r.mode), "")
    return PrintResult(mode=mode, queued=queued) if mode or queued else None


class Job:
    """Print `label` with `options` on `backend`; `printed` counts the labels
    sent so far, also when a later copy fails."""

    def __init__(
        self,
        backend: Backend,
        label: Label,
        options: PrintOptions,
        safe_mm: float = DEFAULT_SAFE_MM,
        values: Values | None = None,
    ):
        self.backend = backend
        self.label = label
        self.options = options
        self.safe_mm = safe_mm
        self.values = values or Values.for_label(label, datetime.now(), options.copies)
        self.printed = 0
        # each label is filled in once: its UUID is the same in the check,
        # the dry run's listing and the image
        self._expanded: dict[int, tuple[Label, list[RenderWarning]]] = {}

    def expanded(self, copy: int) -> tuple[Label, list[RenderWarning]]:
        """The label of the given copy with its variables filled in, and the
        variables that could not be."""
        if copy not in self._expanded:
            self._expanded[copy] = expand_label(self.label, self.values.for_copy(copy))
        return self._expanded[copy]

    @property
    def per_copy(self) -> bool:
        return self.options.copies > 1 and varies_per_copy(self.label)

    def labels(self) -> Iterator[tuple[int, Label]]:
        """(copy number, label with its values) for each render the job needs."""
        count = self.options.copies if self.per_copy else 1
        for copy in range(1, count + 1):
            yield copy, self.expanded(copy)[0]

    def check(self) -> list[RenderWarning]:
        """The problems of the first label: variables that cannot be filled
        in and what the preview would point out."""
        label, warnings = self.expanded(1)
        caps = self.backend.capabilities
        rendered = render(label, caps.dpi, safe_mm=self.safe_mm, min_length_mm=caps.min_length_mm)
        return warnings + rendered.warnings

    def images(self) -> Iterator[tuple[int, Image.Image]]:
        """(copy number, image as the printer gets it) for each render."""
        options = replace(self.options, copies=1) if self.per_copy else self.options
        for copy, label in self.labels():
            try:
                image = self.backend.prepare(label, options, self.safe_mm)
            except Exception as e:  # noqa: BLE001 - whatever went wrong while drawing
                raise PrepareFailed(str(e)) from e
            yield copy, image

    def run(self, host: str, progress: ProgressCallback | None = None) -> PrintResult | None:
        """Print; raises PrepareFailed or the backend's PrinterError."""
        if not self.per_copy:
            _copy, image = next(self.images())
            result = self.backend.print_label(image, host, self.options, progress)
            self.printed = self.options.copies
            return result
        single = replace(self.options, copies=1)
        results = []
        for copy, image in self.images():
            results.append(
                self.backend.print_label(image, host, single, self._progress(progress, copy))
            )
            self.printed = copy
        return merge_results(results)

    def _progress(self, progress: ProgressCallback | None, copy: int):
        """The backend reports copy 1 of 1: tell the caller which one it is."""
        if progress is None:
            return None
        copies = self.options.copies
        return lambda event: progress(Progress(copy, copies, event.stage))

    def next_counter(self) -> int | None:
        """The counter for the next print, None if the label has none."""
        if not uses_counter(self.label):
            return None
        return self.values.counter + self.values.step * self.printed
