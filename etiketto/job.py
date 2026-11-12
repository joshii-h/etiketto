"""A print job: variables filled in per label, rendered and sent to a backend.

Used by the window and by the command line. Without variables that change
from label to label the label is rendered once and the printer makes the
copies; with them (counter, copy number, UUID, ID) every copy is rendered
and sent on its own, so five copies are five numbers. On a label sheet
(page printers) each position is one copy: the pages are composed from the
same image, or from one render per position.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from datetime import datetime

from PIL import Image

from etiketto.backends.base import (
    Backend,
    PrinterError,
    PrintOptions,
    PrintResult,
    Progress,
    ProgressCallback,
    RenderAgain,
)
from etiketto.model import Label
from etiketto.render import DEFAULT_SAFE_MM, RenderWarning, render
from etiketto.sheets.layout import Copies, LabelSource, Pages, SheetJob
from etiketto.variables import Values, expand_label, uses_counter, varies_per_copy


class PrepareFailed(Exception):
    """The label could not be rendered for printing; the cause is chained."""


def merge_results(results: list[PrintResult | None]) -> PrintResult | None:
    """One result for the copies sent one by one."""
    queued = any(r.queued for r in results if r)
    unconfirmed = any(r.unconfirmed for r in results if r)
    mode, reason = next(((r.mode, r.reason) for r in results if r and r.mode), ("", ""))
    if not (mode or queued or unconfirmed):
        return None
    return PrintResult(mode=mode, queued=queued, unconfirmed=unconfirmed, reason=reason)


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
        columns: Mapping[str, str] | None = None,
        sheet: SheetJob | None = None,
    ):
        """sheet: print on label sheets; options.copies is then the number
        of labels (sheet.count)."""
        self.backend = backend
        self.sheet = sheet
        self.label = label
        self.options = options
        self.safe_mm = safe_mm
        self.values = values or Values.for_label(label, datetime.now(), options.copies)
        self.columns = columns  # serial labels: the row the placeholders are filled from
        self.printed = 0
        # the first label is filled in once: its UUID is the same in the
        # check and the print; the others are made as they are needed, a
        # job of many copies does not keep them all
        self._first: tuple[Label, list[RenderWarning]] | None = None

    def expanded(self, copy: int) -> tuple[Label, list[RenderWarning]]:
        """The label of the given copy with its variables filled in, and the
        variables that could not be."""
        if copy != 1:
            return expand_label(self.label, self.values.for_copy(copy), self.columns)
        if self._first is None:
            self._first = expand_label(self.label, self.values, self.columns)
        return self._first

    @property
    def per_copy(self) -> bool:
        return self.options.copies > 1 and varies_per_copy(self.label)

    @property
    def renders(self) -> int:
        """How many images the job needs: one, or one per copy."""
        return self.options.copies if self.per_copy else 1

    def labels(self) -> Iterator[tuple[int, Label]]:
        """(copy number, label with its values) for each render the job needs."""
        for copy in range(1, self.renders + 1):
            yield copy, self.expanded(copy)[0]

    def check(self) -> list[RenderWarning]:
        """The problems of the first label: variables that cannot be filled
        in and what the preview would point out."""
        label, warnings = self.expanded(1)
        caps = self.backend.capabilities
        rendered = render(
            label,
            caps.dpi,
            safe_mm=self.safe_mm,
            min_length_mm=caps.min_length_mm,
            mono=self.backend.renders_mono(self.options.mode),
        )
        return warnings + rendered.warnings

    def images(self) -> Iterator[tuple[int, Image.Image]]:
        """(copy number, image as the printer gets it) for each render."""
        for copy, label in self.labels():
            yield copy, self.image(label)

    def image(self, label: Label) -> Image.Image:
        """The image the printer gets for one label of the job (labels())."""
        options = replace(self.options, copies=1) if self.per_copy else self.options
        try:
            return self.backend.prepare(label, options, self.safe_mm)
        except Exception as e:  # noqa: BLE001 - whatever went wrong while drawing
            raise PrepareFailed(str(e)) from e

    def run(self, host: str, progress: ProgressCallback | None = None) -> PrintResult | None:
        """Print; raises PrepareFailed or the backend's PrinterError.

        `printed` counts the labels that came out: those sent before a
        failure, and those a backend reports in PrinterError.params
        ["printed"] for the failed send (copies of one job before the
        problem). The error then carries "printed" and "copies" of the
        whole job. Interrupted (Ctrl+C) during a send, the labels of that
        send count as printed: the printer may have them, and a number
        skipped is better than one printed twice."""
        if not self.options.host:  # prepare() renders for this printer
            self.options = replace(self.options, host=host)
        if self.sheet is not None:
            return self._run_sheets(self.sheet, host, progress)
        if not self.per_copy:
            label = self.expanded(1)[0]
            return self._send(self.image(label), label, host, self.options, progress)
        single = replace(self.options, copies=1)
        results = []
        for copy, label in self.labels():
            progress_of_copy = self._progress(progress, copy)
            results.append(self._send(self.image(label), label, host, single, progress_of_copy))
        return merge_results(results)

    def _send(
        self, image, label: Label, host: str, options: PrintOptions, progress
    ) -> PrintResult | None:
        try:
            result = self._print_label(image, label, host, options, progress)
        except PrinterError as e:
            done = e.params.get("printed", 0)
            if isinstance(done, int) and not isinstance(done, bool):
                self.printed += max(0, min(done, options.copies))
            if self.printed:
                e.params.update(printed=self.printed, copies=self.options.copies)
            raise
        except Exception:
            raise
        except BaseException:  # KeyboardInterrupt, SystemExit
            self.printed += options.copies
            raise
        self.printed += options.copies
        return result

    def _print_label(self, image, label: Label, host: str, options, progress):
        """Print; once more, rendered anew, if the printer reported another
        resolution than the image was made for (RenderAgain)."""
        try:
            return self.backend.print_label(image, host, options, progress)
        except RenderAgain:
            return self.backend.print_label(self.image(label), host, options, progress)

    def _run_sheets(
        self, sheet: SheetJob, host: str, progress: ProgressCallback | None
    ) -> PrintResult | None:
        """The labels on sheets. A backend's error counts printed sheets
        (params "pages"): the labels on them count as printed."""
        first = None if self.per_copy else self.image(self.expanded(1)[0])
        source: LabelSource = _EachCopy(self) if first is None else Copies(first)
        slots = sheet.slots()
        pages = Pages(sheet, source, self.backend.capabilities.dpi)
        try:
            result = self.backend.print_pages(pages, host, self.options, progress)
        except PrinterError as e:
            done = e.params.get("printed", 0)
            if isinstance(done, int) and not isinstance(done, bool):
                self.printed += sum(len(page) for page in slots[: max(0, done)])
            raise
        except Exception:
            raise
        except BaseException:  # KeyboardInterrupt, SystemExit: see run()
            self.printed += sum(len(page) for page in slots)
            raise
        self.printed += sum(len(page) for page in slots)
        return result

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


class _EachCopy:
    """A label source for a sheet with variables that change per label:
    position n (counted over all sheets) is copy n + 1, rendered on its own;
    at_dpi(): the same, rendered at another resolution (render_at, see
    Copies)."""

    def __init__(self, job: Job, dpi: float | None = None):
        self.job, self.dpi = job, dpi

    def __call__(self, n: int) -> Image.Image:
        image = self.job.image(self.job.expanded(n + 1)[0])
        render_at = image.info.get("render_at")
        if self.dpi is not None and callable(render_at):
            return render_at(self.dpi)
        return image

    def at_dpi(self, dpi: float) -> _EachCopy | None:
        if not callable(self(0).info.get("render_at")):
            return None
        return _EachCopy(self.job, dpi)
