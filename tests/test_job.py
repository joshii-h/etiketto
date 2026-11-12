"""Print jobs: a render per copy when the variables change, counters across
copies, progress and results, failures half-way."""

from __future__ import annotations

from datetime import datetime

import pytest
from fake_backends import FakeMono

from tavletta.backends.base import PaperJam, PrintOptions, PrintResult, Progress
from tavletta.job import Job, PrepareFailed, merge_results
from tavletta.model import CodeElement, Label, TextElement
from tavletta.sheets import find
from tavletta.sheets.layout import SheetJob
from tavletta.variables import Values

NOW = datetime(2026, 3, 1, 9, 30)


class Recording(FakeMono):
    """Remembers the filled-in labels it renders and fails on request."""

    def __init__(self):
        super().__init__()
        self.prepared: list[Label] = []
        self.fail_copy = 0  # the print_label call (1-based) that fails
        self.results: list[PrintResult | None] = []
        self.calls = 0

    def prepare(self, label, options, safe_mm=1.5):
        self.prepared.append(label)
        return super().prepare(label, options, safe_mm)

    def print_label(self, image, host, options, progress=None):
        self.calls += 1
        if self.calls == self.fail_copy:
            raise PaperJam("jam")
        super().print_label(image, host, options, progress)
        return self.results.pop(0) if self.results else None


def texts(labels: list[Label]) -> list[str]:
    return [label.elements[0].text for label in labels]


def make_job(template: str, copies: int, backend=None, **label_fields) -> Job:
    label = Label(tape_mm=12, elements=[TextElement(text=template)], **label_fields)
    options = PrintOptions(copies=copies, cut="")
    values = Values.for_label(label, NOW, copies)
    return Job(backend or Recording(), label, options, values=values)


def test_without_changing_variables_the_printer_makes_the_copies():
    job = make_job("Packed {=date:%d.%m.%Y}, {=copies} pieces", 5)
    assert not job.per_copy
    result = job.run("mono.local")
    backend = job.backend
    assert result is None
    assert texts(backend.prepared) == ["Packed 01.03.2026, 5 pieces"]
    assert len(backend.jobs) == 5  # the fake prints copies × the one image
    assert backend.jobs[0][2].copies == 5
    assert job.printed == 5
    assert job.next_counter() is None  # no counter on the label


def test_counter_across_copies():
    job = make_job("No. {=counter:000} ({=copy}/{=copies})", 3, counter_next=41, counter_step=2)
    assert job.per_copy
    events: list[Progress] = []
    job.run("mono.local", events.append)
    backend = job.backend
    assert texts(backend.prepared) == ["No. 041 (1/3)", "No. 043 (2/3)", "No. 045 (3/3)"]
    assert [options.copies for _image, _host, options in backend.jobs] == [1, 1, 1]
    assert [(e.copy, e.copies) for e in events] == [(1, 3), (2, 3), (3, 3)]
    assert job.printed == 3
    assert job.next_counter() == 47
    assert job.label.counter_next == 41  # the job never changes the label


def test_a_failing_copy_keeps_the_numbers_printed_before():
    backend = Recording()
    backend.fail_copy = 3
    job = make_job("{=counter}", 4, backend, counter_next=10)
    with pytest.raises(PaperJam):
        job.run("mono.local")
    assert job.printed == 2
    assert job.next_counter() == 12  # 10 and 11 are on labels now


def test_a_single_copy_is_one_render():
    job = make_job("{=counter}", 1, counter_next=5)
    assert not job.per_copy
    job.run("mono.local", None)
    assert texts(job.backend.prepared) == ["5"]
    assert job.next_counter() == 6


def test_the_same_values_in_check_images_and_listing():
    job = make_job("{=uuid}", 2)
    first = job.expanded(1)[0].elements[0].text
    assert job.check() == []
    listed = []
    for _copy, label in job.labels():  # the dry run: one label, its listing and image
        listed.append(label.elements[0].text)
        job.image(label)
    assert listed[0] == first and texts(job.backend.prepared) == listed
    assert listed[0] != listed[1]
    assert [copy for copy, _image in job.images()] == [1, 2]
    assert texts(job.backend.prepared)[2] == first  # the first label keeps its values


def test_check_reports_unknown_variables_and_render_problems():
    label = Label(
        tape_mm=12,
        elements=[TextElement(text="{=lot}"), CodeElement(data="{=copy}x", symbology="ean13")],
    )
    job = Job(Recording(), label, PrintOptions())
    assert [w.key for w in job.check()] == ["unknown_variable", "ean13_digits"]


def test_prepare_failures_are_told_apart(monkeypatch):
    backend = Recording()

    def broken(*_args, **_kwargs):
        raise RuntimeError("cairo")

    monkeypatch.setattr(backend, "prepare", broken)
    job = make_job("{=counter}", 2, backend)
    with pytest.raises(PrepareFailed) as info:
        job.run("mono.local")
    assert isinstance(info.value.__cause__, RuntimeError)
    assert job.printed == 0


def test_results_of_single_copies_are_merged():
    backend = Recording()
    backend.results = [None, PrintResult(mode="color"), PrintResult(queued=True)]
    job = make_job("{=copy}", 3, backend)
    assert job.run("mono.local") == PrintResult(mode="color", queued=True)
    assert merge_results([None, None]) is None


def test_values_default_to_now():
    label = Label(tape_mm=12, elements=[TextElement(text="{=counter}")], counter_next=3)
    job = Job(Recording(), label, PrintOptions(copies=2))
    assert job.values.counter == 3 and job.values.copies == 2
    assert abs((job.values.now - datetime.now()).total_seconds()) < 60


class PartlyPrinted(Recording):
    """Fails with the copies it printed before the problem, as the system
    printer reports them (PrinterError.params printed/copies)."""

    def __init__(self, printed, with_params=True):
        super().__init__()
        self.printed = printed
        self.with_params = with_params

    def print_label(self, image, host, options, progress=None):
        self.calls += 1
        if self.calls == self.fail_copy:
            error = PaperJam("jam")
            if self.with_params:
                error.params.update(printed=self.printed, copies=options.copies)
            raise error
        return


def test_copies_a_backend_printed_before_failing_count():
    job = make_job("{=counter}", 5, PartlyPrinted(3))
    job.label.elements[0].text = "{=date}"  # the printer makes the copies
    job.backend.fail_copy = 1
    with pytest.raises(PaperJam) as e:
        job.run("h")
    assert job.printed == 3
    assert e.value.params == {"printed": 3, "copies": 5}


def test_a_failure_after_some_copies_reports_them_all():
    job = make_job("{=counter}", 4, PartlyPrinted(0))
    job.backend.fail_copy = 3
    with pytest.raises(PaperJam) as e:
        job.run("h")
    assert job.printed == 2 and job.next_counter() == 3
    assert e.value.params == {"printed": 2, "copies": 4}
    # a backend that does not report it: the copies sent before still count
    job = make_job("{=counter}", 4, PartlyPrinted(0, with_params=False))
    job.backend.fail_copy = 3
    with pytest.raises(PaperJam) as e:
        job.run("h")
    assert e.value.params == {"printed": 2, "copies": 4}
    # nothing printed: nothing added
    job = make_job("{=counter}", 4, PartlyPrinted(0, with_params=False))
    job.backend.fail_copy = 1
    with pytest.raises(PaperJam) as e:
        job.run("h")
    assert e.value.params == {} and job.printed == 0


@pytest.mark.parametrize("printed", [99, -1, True, "2"])
def test_odd_printed_counts_are_kept_in_bounds(printed):
    job = make_job("{=date}", 3, PartlyPrinted(printed))
    job.backend.fail_copy = 1
    with pytest.raises(PaperJam):
        job.run("h")
    assert job.printed == (3 if printed == 99 else 0)


def test_interrupted_while_sending_the_label_counts_as_printed():
    """The printer may have it: a number skipped, not one printed twice."""

    class Interrupted(Recording):
        def print_label(self, image, host, options, progress=None):
            self.calls += 1
            if self.calls == 2:
                raise KeyboardInterrupt
            return

    job = make_job("{=counter}", 3, Interrupted())
    with pytest.raises(KeyboardInterrupt):
        job.run("h")
    assert job.printed == 2 and job.next_counter() == 3


def test_only_the_first_label_is_kept():
    job = make_job("{=uuid}", 3)
    first = job.expanded(1)
    assert job.expanded(1) is first
    assert job.expanded(2)[0].elements[0].text != job.expanded(2)[0].elements[0].text
    assert job.renders == 3


class SheetRecording(Recording):
    """A page printer: remembers the pages and the labels on them."""

    def __init__(self):
        super().__init__()
        self.pages: list = []
        self.fail_page = 0  # the page (1-based) that fails, its sheets before printed

    def print_pages(self, pages, host, options, progress=None):
        for n, page in enumerate(pages, 1):
            if n == self.fail_page:
                raise PaperJam("jam", printed=n - 1, copies=len(pages), pages=True)
            self.pages.append(page)
        return


def sheet_job(template: str, labels: int, backend=None) -> Job:
    sheet = find("Avery L7160")  # 21 labels per sheet
    label = Label(tape_mm=12, elements=[TextElement(text=template)])
    options = PrintOptions(copies=labels, cut="")
    values = Values.for_label(label, NOW, labels)
    return Job(
        backend or SheetRecording(), label, options, values=values, sheet=SheetJob(sheet, labels)
    )


def test_on_label_sheets_each_position_gets_its_own_number():
    job = sheet_job("No. {=counter}", 23)
    job.run("office.local")
    backend = job.backend
    assert len(backend.pages) == 2  # 21 + 2 labels
    assert texts(backend.prepared) == ["No. %d" % n for n in range(1, 24)]
    assert job.printed == 23 and job.next_counter() == 24


def test_on_label_sheets_without_changing_variables_one_render():
    job = sheet_job("Packed {=date:%d.%m.%Y}", 23)
    job.run("office.local")
    assert texts(job.backend.prepared) == ["Packed 01.03.2026"]
    assert len(job.backend.pages) == 2


def test_a_failed_sheet_counts_the_labels_of_the_sheets_printed():
    backend = SheetRecording()
    backend.fail_page = 2
    job = sheet_job("{=counter}", 30, backend)
    with pytest.raises(PaperJam):
        job.run("office.local")
    assert job.printed == 21 and job.next_counter() == 22


def test_per_label_renders_on_sheets_render_again_at_another_resolution():
    from tavletta.job import _EachCopy

    class Rerendering(SheetRecording):
        def prepare(self, label, options, safe_mm=1.5):
            image = super().prepare(label, options, safe_mm)
            image.info["render_at"] = lambda dpi: image.resize((int(dpi), 1))
            return image

    job = sheet_job("{=counter}", 2, Rerendering())
    source = _EachCopy(job).at_dpi(42)
    assert source is not None and source(1).size == (42, 1)
    assert texts(job.backend.prepared)[-1] == "2"
    assert _EachCopy(sheet_job("{=counter}", 2)).at_dpi(42) is None


def test_a_printer_with_another_resolution_gets_the_label_rendered_again():
    from tavletta.backends.base import RenderAgain

    class Changing(Recording):
        def print_label(self, image, host, options, progress=None):
            if not self.prepared[1:]:  # the first image was made for another resolution
                raise RenderAgain("printer reported 300 dpi")
            return super().print_label(image, host, options, progress)

    job = make_job("{=date:%Y}", 1, Changing())
    job.run("zebra.local")
    assert texts(job.backend.prepared) == ["2026", "2026"]
    assert job.printed == 1 and job.options.host == "zebra.local"


def test_unconfirmed_copies_are_merged():
    assert merge_results([None, PrintResult(unconfirmed=True)]) == PrintResult(unconfirmed=True)
    assert merge_results([None, None]) is None


def test_the_check_renders_as_the_print(monkeypatch):
    """Job.check() renders with the backend's render_label() and the job's
    options: the printer's resolution, tape and reduction, as printed."""
    backend = Recording()
    seen = []
    real = backend.render_label
    monkeypatch.setattr(
        backend,
        "render_label",
        lambda label, options, safe_mm: seen.append(options) or real(label, options, safe_mm),
    )
    job = make_job("{=date:%Y}", 1, backend)
    assert job.check() == []
    assert seen == [job.options]


def test_an_export_renders_each_label_once(monkeypatch):
    """Job.export (the dry run's --output) draws a label once, as the
    window's export does, not once for the printer and once more."""
    from tavletta.model import TextElement

    backend = FakeMono()
    renders = []
    real = backend.render_label
    monkeypatch.setattr(
        backend, "render_label", lambda *a, **k: renders.append(1) or real(*a, **k)
    )
    label = Label(printer=backend.id, tape_mm=12, elements=[TextElement(text="{=copy}")])
    job = Job(backend, label, PrintOptions(copies=3), values=Values.for_label(label, NOW, 3))
    for _copy, each in job.labels():
        job.export(each)
    assert len(renders) == 3


def test_an_export_is_the_windows_preview_image():
    """The same function, the same cap: a page printer reporting 1200 dpi
    exports at PREVIEW_MAX_DPI, as the window does."""
    from tavletta.backends import get_backend
    from tavletta.backends.base import PrinterInfo
    from tavletta.job import PREVIEW_MAX_DPI, preview_image
    from tavletta.model import TextElement

    backend = get_backend("system")
    label = Label(printer="system", orientation="across", auto_length=False)
    label.set_media_size(100, 50)
    label.elements = [TextElement(text="Etikett")]
    options = PrintOptions(info=PrinterInfo("Office", None, None, None, "IDLE", dpi=1200))
    image, dpi = Job(backend, label, options).export(label)
    assert dpi == PREVIEW_MAX_DPI
    _rendered, window, window_dpi = preview_image(backend, label, options)
    assert window_dpi == dpi and image.tobytes() == window.tobytes()


def test_an_export_that_cannot_be_drawn_fails_as_prepare_does(monkeypatch):
    backend = FakeMono()

    def broken(*_args, **_kwargs):
        raise RuntimeError("kaputt")

    monkeypatch.setattr(backend, "render_label", broken)
    label = Label(printer=backend.id, tape_mm=12)
    with pytest.raises(PrepareFailed, match="kaputt"):
        Job(backend, label, PrintOptions()).export(label)
