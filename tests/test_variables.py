"""Variables in texts and codes: syntax, dates on a fixed clock, counters,
random values, and what the label file keeps."""

from __future__ import annotations

import json
import locale
import uuid
from datetime import datetime
from itertools import count

import pytest

from etiketto.model import CodeElement, ImageElement, Label, LabelError, TextElement
from etiketto.render import RenderWarning
from etiketto.variables import (
    ID_ALPHABET,
    Values,
    column_names,
    expand,
    expand_label,
    label_names,
    names,
    resolve,
    shift,
    uses_counter,
    varies_per_copy,
)

NOW = datetime(2026, 1, 31, 14, 5, 9)


def fixed_bytes(n: int) -> bytes:
    return bytes(range(n))


def values(**kwargs) -> Values:
    kwargs.setdefault("random", fixed_bytes)
    return Values(NOW, **kwargs)


def text(template: str, **kwargs) -> str:
    result, unknown = expand(template, values(**kwargs))
    assert unknown == []
    return result


# --- dates and times -----------------------------------------------------------


@pytest.mark.parametrize(
    "template, expected",
    [
        ("{=date:%d.%m.%Y}", "31.01.2026"),
        ("{=date:%Y-%m-%d}", "2026-01-31"),
        ("{=date+30d:%d.%m.%Y}", "02.03.2026"),
        ("{=date-1d:%d.%m.}", "30.01."),
        ("{=date+2w:%d.%m.}", "14.02."),
        ("{=date+1m:%d.%m.%Y}", "28.02.2026"),  # the 31st: the last day of February
        ("{=date+13m:%d.%m.%Y}", "28.02.2027"),
        ("{=date-2m:%d.%m.%Y}", "30.11.2025"),
        ("{=date+1y:%d.%m.%Y}", "31.01.2027"),
        ("{=date+1m-1d:%d.%m.%Y}", "27.02.2026"),
        ("{=date + 3 D:%d}", "03"),  # spaces and upper case
        ("{=time:%H:%M}", "14:05"),  # the format may contain colons
        ("{=time+2h:%H:%M}", "16:05"),
        ("{=date:}", ""),
        ("Best before {=date+30d:%d.%m.%Y}.", "Best before 02.03.2026."),
    ],
)
def test_dates_on_a_fixed_clock(template, expected):
    assert text(template) == expected


def test_leap_day_plus_a_year():
    assert shift(datetime(2028, 2, 29), "+1y").replace(tzinfo=None) == datetime(2029, 2, 28)
    assert shift(datetime(2028, 2, 29), "+4y").replace(tzinfo=None) == datetime(2032, 2, 29)


def test_default_formats_follow_the_locale():
    assert text("{=date}") == NOW.strftime("%x")
    assert text("{=time}") == NOW.strftime("%X")
    previous = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, "de_CH.UTF-8")
    except locale.Error:
        pytest.skip("no de_CH locale")
    try:
        assert text("{=date}") == "31.01.2026"
    finally:
        locale.setlocale(locale.LC_TIME, previous)


@pytest.mark.parametrize(
    "spec",
    [
        "date+3x",  # unknown unit
        "date+99999y",  # past the year 9999
        "date+999999999999999999999d",  # too large for a timedelta
        "counter+1d",  # numbers take no offset
        "counter:abc",
        "counter:" + "0" * 31,
        "uuid:x",
        "uuid+1d",
        "id:0",
        "id:65",
        "id:x",
        "id+1d",
        "nope",
        "1date",
        "",
        "date:%",  # strftime with a dangling %: platform-dependent, never a crash
    ],
)
def test_invalid_specs(spec):
    result = resolve(spec, values())
    assert result is None or isinstance(result, str)
    if spec != "date:%":
        assert result is None


# --- counters, copies, random values -------------------------------------------------


def test_counter_and_copies():
    assert text("{=counter}") == "1"
    assert text("{=counter:0001}", counter=7) == "0007"
    assert text("{=counter:00}", counter=1234) == "1234"  # never cut off
    assert text("{=counter}", counter=10, step=5, copy=3) == "20"
    assert text("{=copy:00}/{=copies}", copy=2, copies=5) == "02/5"


def test_each_copy_gets_the_next_number():
    start = values(counter=100, step=2, copies=3)
    numbers = [expand("{=counter}", start.for_copy(n))[0] for n in (1, 2, 3)]
    assert numbers == ["100", "102", "104"]


def test_uuid_and_id_are_the_same_within_one_label():
    one = values()
    first = text("{=uuid} {=uuid}")
    a, b = first.split()
    assert a == b
    assert uuid.UUID(a).version == 4
    ids = expand("{=id} {=id:4} {=id:12}", one)[0].split()
    assert ids[0][:4] == ids[1] and ids[2].startswith(ids[0])
    assert len(ids[0]) == 8 and set("".join(ids)) <= set(ID_ALPHABET)


def test_random_values_change_from_copy_to_copy():
    calls = count()

    def random(n: int) -> bytes:
        start = next(calls)
        return bytes((start + i) % 256 for i in range(n))

    source = values(random=random)
    first, second = source.for_copy(1), source.for_copy(2)
    assert expand("{=uuid}", first)[0] != expand("{=uuid}", second)[0]
    assert expand("{=id}", first)[0] != expand("{=id}", second)[0]


def test_custom_values():
    assert text("Batch {=batch}", custom={"batch": "A12"}) == "Batch A12"
    assert resolve("batch:x", values(custom={"batch": "A12"})) is None
    assert resolve("batch", values()) is None


# --- the syntax ----------------------------------------------------------------


def test_texts_without_variables_stay_as_they_are():
    # labels from before variables: "{{" stays, plain braces stay
    for plain in ("{{x}}", "{Name}", "}{", "a {b} c", ""):
        assert expand(plain, values()) == (plain, [])


def test_escaped_braces_and_unknown_variables():
    assert text("{{=date}} {=copy}") == "{=date} 1"
    assert text("{{x}} {=copy}") == "{x} 1"
    assert text("{Name} {=copy}") == "{Name} 1"  # a column without serial labels: kept
    result, unknown = expand("{=nope} and {=date+1q} {=copy}", values())
    assert result == "{=nope} and {=date+1q} 1"
    assert unknown == ["{=nope}", "{=date+1q}"]


def test_columns_and_variables_in_one_pass():
    """Serial labels: "{Column}" from the data, "{=name}" computed; a column
    named like a variable never shadows it."""
    row = {"Name": "Ada", "date": "column", "counter": "column"}
    result, unknown = expand(
        "{Name} {=counter:000} {date} {=date:%Y} {{=date}} {Missing}", values(counter=5), row
    )
    assert result == "Ada 005 column 2026 {=date} {Missing}"
    assert unknown == ["{Missing}"]
    # filled in one pass: a value is never read as a placeholder again
    assert expand("{Name}", values(), {"Name": "{=date}"})[0] == "{=date}"


def test_names():
    assert names("plain") == set()
    assert names("{=date+1d:%d} {=counter} {=batch} {{=uuid}} {=}") == {"date", "counter", "batch"}


# --- labels --------------------------------------------------------------------


def test_expand_label_keeps_the_original():
    label = Label(
        elements=[
            TextElement(text="No. {=counter:000}"),
            ImageElement(data="", name="{=date}.png"),
            CodeElement(data="https://x/{=counter}"),
            TextElement(text="{=unknown}"),
            TextElement(text="plain"),
        ],
        counter_next=42,
    )
    filled, warnings = expand_label(label, Values.for_label(label, NOW))
    assert [getattr(e, "text", None) or getattr(e, "data", None) for e in filled.elements] == [
        "No. 042",
        "",
        "https://x/42",
        "{=unknown}",
        "plain",
    ]
    assert filled.elements[1].name == "{=date}.png"  # an image's file name is no template
    assert warnings == [RenderWarning("unknown_variable", {"n": 4, "name": "{=unknown}"})]
    assert label.elements[0].text == "No. {=counter:000}"
    assert label_names(label) == {"counter", "unknown"}
    assert uses_counter(label) and varies_per_copy(label)
    assert filled.elements[4] is label.elements[4]


def test_what_varies_per_copy():
    def label(*texts):
        return Label(elements=[TextElement(text=t) for t in texts])

    assert not varies_per_copy(label("{=date}", "{=copies}", "{=time}"))
    for name in ("counter", "copy", "uuid", "id"):
        assert varies_per_copy(label("{=%s}" % name))
    assert not uses_counter(label("{=copy}"))


# --- the label file ------------------------------------------------------------


def test_counter_and_names_are_saved(tmp_path):
    label = Label(
        elements=[TextElement(text="{=counter}", name="serial"), CodeElement(name="link")],
        counter_next=17,
        counter_step=3,
    )
    path = tmp_path / "a.etiketto"
    label.save(path)
    loaded = Label.load(path)
    assert (loaded.counter_next, loaded.counter_step) == (17, 3)
    assert [e.name for e in loaded.elements] == ["serial", "link"]


def test_old_files_load_with_defaults():
    label = Label.from_dict({"elements": [{"kind": "text", "text": "x"}], "version": 1})
    assert (label.counter_next, label.counter_step) == (1, 1)
    assert label.elements[0].name == ""


@pytest.mark.parametrize(
    "field, value, expected",
    [
        ("counter_next", -5, 0),
        ("counter_next", 10**20, 10**15 - 1),
        ("counter_step", 0, 1),
        ("counter_step", 10**9, 10**6),
    ],
)
def test_counter_values_are_clamped(field, value, expected):
    assert getattr(Label.from_dict({field: value}), field) == expected


@pytest.mark.parametrize("value", [1.5, "3", True, None])
def test_counter_must_be_a_whole_number(value):
    with pytest.raises(LabelError) as info:
        Label.from_dict({"counter_next": value})
    assert info.value.code == "damaged"


def test_element_name_must_be_text():
    with pytest.raises(LabelError):
        Label.from_dict({"elements": [{"kind": "code", "name": 5}]})


def test_store_counter_changes_only_the_counter(tmp_path):
    path = tmp_path / "a.etiketto"
    Label(elements=[TextElement(text="{=counter}")], counter_next=5).save(path)
    before = json.loads(path.read_text())
    Label.store_counter(path, 9)
    after = json.loads(path.read_text())
    assert after.pop("counter_next") == 9
    before.pop("counter_next")
    assert after == before
    path.write_text("[]")
    with pytest.raises(LabelError):
        Label.store_counter(path, 1)


# --- review fixes ----------------------------------------------------------------


@pytest.mark.parametrize(
    "template, fragments",
    [
        ("Lot {=lot", ["{=lot"]),
        ("{={=date:%Y}}", ["{="]),
        ("{=date:%Y} and {=", ["{="]),
        ("a {=b c", ["{=b"]),
    ],
)
def test_variables_not_closed_are_reported(template, fragments):
    result, unknown = expand(template, values())
    assert unknown == fragments
    label = Label(elements=[TextElement(text=template)])
    _label, warnings = expand_label(label, values())
    assert {w.key for w in warnings} == {"unclosed_variable"}


def test_escaped_braces_are_not_unclosed():
    assert expand("{{=date", values()) == ("{=date", [])
    assert expand("{{={=copy}}}", values()) == ("{=1}", [])


def test_a_date_format_that_gives_nothing_is_not_a_variable():
    assert resolve("date:%Y", values()) == "2026"
    assert resolve("date:", values()) == ""  # an empty format asks for nothing
    assert resolve("date:%Ez", values()) is None  # glibc: an unknown format, empty
    assert names("{=date:%Ez}") == set()


@pytest.fixture
def zurich(monkeypatch):
    """The system's time zone: Zurich, summer time from 29 March 2026 2:00."""
    import time

    monkeypatch.setenv("TZ", "Europe/Zurich")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_hours_across_the_change_to_summer_time(zurich):
    night = datetime(2026, 3, 29, 1, 30)
    later = shift(night, "+1h")
    assert later.strftime("%H:%M %z") == "03:30 +0200"  # 2:30 does not exist that night
    assert shift(night, "+1d").strftime("%d. %H:%M %z") == "30. 01:30 +0200"
    assert shift(datetime(2026, 3, 28, 12), "+1d").strftime("%H:%M") == "12:00"
    assert shift(datetime(2026, 3, 29, 3, 30), "-1h").strftime("%H:%M %z") == "01:30 +0100"
    assert resolve("time+2h:%H:%M %Z", Values(night)) == "04:30 CEST"
    assert resolve("date:%z", Values(datetime(2026, 1, 1))) == "+0100"


def test_a_given_offset_is_kept():
    from datetime import timedelta, timezone

    moment = datetime(2026, 3, 29, 1, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert shift(moment, "+1h+1d").strftime("%d %H:%M %z") == "30 02:30 -0500"


def test_names_in_any_language():
    assert names("{=Größe} {=数量} {=_x1}") == {"Größe", "数量", "_x1"}
    assert expand("{=Größe}", values(custom={"Größe": "XL"})) == ("XL", [])
    assert names("{=1a}") == set()


def test_invalid_uses_are_not_counted_as_names():
    assert names("{=counter+1d} {=date:%Y} {=lot:x} {=lot+1d} {=id:0}") == {"date"}
    assert not uses_counter(Label(elements=[TextElement(text="{=counter:abc}")]))
    assert names("{=copy:0000}") == {"copy"}


def test_format_codes_are_checked():
    assert resolve("date:%Q", values()) is None  # glibc would print "%Q"
    assert resolve("date:%", values()) is None
    assert resolve("date:%d.%m.%Y %%", values()) == "31.01.2026 %"
    assert resolve("date:%-d.%Ey %Od", values()) is not None
    assert names("{=date:%Q}") == set()


def test_a_format_may_give_nothing_on_purpose():
    """%p is empty in languages without AM/PM, e.g. de_CH: still a value."""
    assert resolve("time:%p", values()) is not None
    assert names("{=time:%p}") == {"time"}


def test_dates_out_of_range():
    from etiketto.variables import problem

    assert resolve("date+9000y", values()) is None
    assert problem("date+9000y", values()) == "date_out_of_range"
    assert problem("date-2026y", values()) == "date_out_of_range"
    assert problem("date-800000d", values()) == "date_out_of_range"
    assert problem("nope", values()) == "unknown_variable"
    label = Label(elements=[TextElement(text="{=date+9000y}")])
    _label, warnings = expand_label(label, values())
    assert [w.key for w in warnings] == ["date_out_of_range"]


def test_hours_with_a_zone_are_real_hours():
    from zoneinfo import ZoneInfo

    night = datetime(2026, 10, 25, 1, 30, tzinfo=ZoneInfo("Europe/Zurich"))
    assert shift(night, "+1h").strftime("%H:%M %z") == "02:30 +0200"
    assert shift(night, "+2h").strftime("%H:%M %z") == "02:30 +0100"  # the hour again
    assert shift(night, "+1d").strftime("%d %H:%M %z") == "26 01:30 +0100"


def test_hours_into_the_repeated_hour_at_the_end_of_summer_time(zurich):
    night = datetime(2026, 10, 25, 1, 30)  # summer time, naive like datetime.now()
    assert shift(night, "+1h").strftime("%H:%M %z") == "02:30 +0200"
    assert shift(night, "+2h").strftime("%H:%M %z") == "02:30 +0100"  # the second 02:30
    assert shift(night, "+3h").strftime("%H:%M %z") == "03:30 +0100"
    second = datetime(2026, 10, 25, 2, 30, fold=1)  # now() in the repeated hour
    assert shift(second, "").strftime("%z") == "+0100"
    assert shift(second, "-1h").strftime("%H:%M %z") == "02:30 +0200"
    assert shift(second, "+1d").strftime("%d %H:%M %z") == "26 02:30 +0100"


# --- columns and variables in one pass (serial labels) --------------------------------


def test_columns_and_variables_are_filled_in_one_pass():
    values = Values(NOW_FOR_COLUMNS, counter=7)
    row = {"Name": "{=counter} {{", "City": "Bern"}
    text, unknown = expand("{Name}/{=counter}/{City}/{Price}/{{x}}", values, row)
    assert text == "{=counter} {{/7/Bern/{Price}/{x}"  # a value is never filled in again
    assert unknown == ["{Price}"]
    # with a data file braces are always escapes; without one, only with "{="
    assert expand("a {{b}}", values, row) == ("a {b}", [])
    assert expand("a {{b}} {}", values, row) == ("a {b} {}", [])
    assert expand("a {{b}} {City}", values, None) == ("a {{b}} {City}", [])
    # "{ =x}" is neither a variable nor a column; without values variables stay
    assert expand("{ =x} {City}", values, {"=x": "no", "City": "Bern"}) == ("{ =x} Bern", [])
    assert expand("{=counter} {City}", None, row) == ("{=counter} Bern", [])


def test_column_placeholders_are_named_but_not_warnings():
    label = Label(elements=[TextElement(text="{Name} {Gone} {=nope}")])
    filled, warnings = expand_label(label, Values(NOW_FOR_COLUMNS), {"Name": "Anna"})
    assert filled.elements[0].text == "Anna {Gone} {=nope}"
    assert [w.params["name"] for w in warnings] == ["{=nope}"]
    assert expand_label(label, None, {"Name": "Anna"})[1] == []
    assert column_names("{A} {=b} {{C}} { A }{ } {D}") == ["A", "D"]


NOW_FOR_COLUMNS = datetime(2026, 3, 1, 9, 30)
