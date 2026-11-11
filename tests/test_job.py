"""Print jobs: a render per copy when the variables change, counters across
copies, progress and results, failures half-way."""

from __future__ import annotations

from datetime import datetime

import pytest
from fake_backends import FakeMono

from etiketto.backends.base import PaperJam, PrintOptions, PrintResult, Progress
from etiketto.job import Job, PrepareFailed, merge_results
from etiketto.model import CodeElement, Label, TextElement
from etiketto.variables import Values

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
    images = list(job.images())
    assert [copy for copy, _image in images] == [1, 2]
    listed = [label.elements[0].text for _copy, label in job.labels()]
    assert listed[0] == first and texts(job.backend.prepared) == listed
    assert listed[0] != listed[1]


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
