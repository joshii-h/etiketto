"""Undo and redo: steps, joined typing, the limit and fields undo keeps."""

from __future__ import annotations

from etiketto.history import COALESCE_SECONDS, LIMIT, History
from etiketto.model import Label, ShapeElement, TextElement


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_undo_and_redo_restore_copies():
    label = Label(elements=[TextElement(text="a")])
    history = History(label)
    assert not history.can_undo and not history.can_redo
    label.elements[0].text = "b"
    history.record(label)
    label.elements.append(ShapeElement())
    history.record(label)
    before = history.undo()
    assert before is not None and len(before.elements) == 1 and before.elements[0].text == "b"
    first = history.undo()
    assert first is not None and first.elements[0].text == "a"
    assert history.undo() is None and history.can_redo
    again = history.redo()
    assert again is not None and again.elements[0].text == "b"
    again.elements[0].text = "changed"  # a copy: the history keeps its own
    assert history.redo().elements[0].text == "b"
    assert history.redo() is None


def test_typing_is_one_step():
    clock = Clock()
    label = Label(elements=[TextElement(text="")])
    history = History(label, clock)
    for letter in "Hello":
        label.elements[0].text += letter
        clock.now += 0.3
        history.record(label, "element:0:text")
    label.elements[0].bold = False
    history.record(label, "element:0:bold")
    assert history.undo().elements[0].text == "Hello"
    assert history.undo().elements[0].text == ""
    assert not history.can_undo


def test_a_pause_starts_a_new_step():
    clock = Clock()
    label = Label(elements=[TextElement(text="a")])
    history = History(label, clock)
    label.elements[0].text = "ab"
    history.record(label, "k")
    clock.now += COALESCE_SECONDS + 0.1
    label.elements[0].text = "abc"
    history.record(label, "k")
    assert history.undo().elements[0].text == "ab"


def test_a_new_change_drops_the_redo_steps_and_undo_breaks_joining():
    clock = Clock()
    label = Label()
    history = History(label, clock)
    label.margin_mm = 3
    history.record(label, "label:margin_mm")
    history.undo()
    label = Label(margin_mm=4)
    history.record(label, "label:margin_mm")
    assert not history.can_redo
    assert history.undo().margin_mm == 2.0


def test_the_history_is_limited():
    label = Label()
    history = History(label)
    for n in range(LIMIT + 20):
        label.counter_step = n + 1
        history.record(label)
    steps = 0
    while history.undo() is not None:
        steps += 1
    assert steps == LIMIT


def test_patch_and_reset():
    label = Label()
    history = History(label)
    label.margin_mm = 5
    history.record(label)
    history.patch("counter_next", 42)
    assert history.undo().counter_next == 42
    assert history.redo().counter_next == 42
    history.reset(Label())
    assert not history.can_undo and not history.can_redo


def test_typing_key_joins_typing_and_numbers_only():
    from etiketto.history import typing_key

    assert typing_key("element:0", {"text": "a"}) == "element:0:text"
    assert typing_key("label", {"margin_mm": 2.5}) == "label:margin_mm"
    assert typing_key("element:0", {"bold": True}) is None
    assert typing_key("element:0", {"align": "right"}) is None
    assert typing_key("element:0", {"color": "#ff0000"}) is None
    assert typing_key("element:0", {}) is None


def test_undo_back_to_the_saved_state():
    label = Label()
    history = History(label)
    assert history.at_saved
    label.margin_mm = 3
    history.record(label)
    assert not history.at_saved
    history.undo()
    assert history.at_saved
    history.redo()
    history.mark_saved()
    assert history.at_saved
    history.undo()
    assert not history.at_saved


def test_replaced_images_are_limited_by_size(monkeypatch):
    from etiketto import history as module
    from etiketto.model import ImageElement

    monkeypatch.setattr(module, "LIMIT_BYTES", 1000)
    label = Label(elements=[ImageElement(data="A" * 300)])
    history = History(label)
    for n in range(10):
        label.elements[0].data = str(n) * 300  # each a new image
        history.record(label)
    steps = 0
    while history.undo() is not None:
        steps += 1
    assert steps == 2  # three images of 300 bytes fit in 1000


def test_shared_images_count_once(monkeypatch):
    from etiketto import history as module
    from etiketto.model import ImageElement

    monkeypatch.setattr(module, "LIMIT_BYTES", 1000)
    label = Label(elements=[ImageElement(data="A" * 600)])
    history = History(label)
    for n in range(10):
        label.margin_mm = n
        history.record(label)
    assert sum(1 for _ in iter(history.undo, None)) == 10


def test_whole_number_choices_are_steps_of_their_own():
    from etiketto.history import typing_key

    assert typing_key("element:0", {"rotation": 90}) is None
    assert typing_key("element:0", {"threshold": 100}) == "element:0:threshold"
    assert typing_key("label", {"margin_mm": 2.0}) == "label:margin_mm"


def test_saving_ends_a_typing_step():
    clock = Clock()
    label = Label(elements=[TextElement(text="a")])
    history = History(label, clock)
    label.elements[0].text = "ab"
    history.record(label, "element:0:text")
    history.mark_saved()
    label.elements[0].text = "abc"
    history.record(label, "element:0:text")  # within the time, but after saving
    assert not history.at_saved
    assert history.undo().elements[0].text == "ab"
    assert history.at_saved
