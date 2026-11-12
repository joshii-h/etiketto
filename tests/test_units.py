import pytest

from tavletta.units import INCHES, MILLIMETRES, default_unit, locale_territory, unit_for


@pytest.mark.parametrize(
    "environ, territory",
    [
        ({"LANG": "en_US.UTF-8"}, "US"),
        ({"LANG": "de_CH"}, "CH"),
        ({"LANG": "sr_RS@latin"}, "RS"),
        ({"LANG": "en_us.utf8"}, "US"),
        ({"LANG": "C"}, None),
        ({"LANG": "C.UTF-8"}, None),
        ({"LANG": ""}, None),
        ({}, None),
        ({"LC_ALL": "en_US.UTF-8", "LANG": "de_DE.UTF-8"}, "US"),
        ({"LC_ALL": "", "LC_MEASUREMENT": "en_LR.UTF-8", "LANG": "de_DE"}, "LR"),
        ({"LC_MEASUREMENT": "de_DE.UTF-8", "LANG": "en_US.UTF-8"}, "DE"),
        ({"LC_ALL": "C", "LANG": "en_US.UTF-8"}, None),  # first set variable decides
    ],
)
def test_locale_territory(environ, territory):
    assert locale_territory(environ) == territory


def test_locale_territory_uses_os_environ(monkeypatch):
    monkeypatch.setenv("LC_ALL", "en_MM.UTF-8")
    assert locale_territory() == "MM"


@pytest.mark.parametrize(
    "environ, unit",
    [
        ({"LANG": "en_US.UTF-8"}, "in"),
        ({"LANG": "en_LR"}, "in"),
        ({"LANG": "my_MM"}, "in"),
        ({"LANG": "en_GB.UTF-8"}, "mm"),
        ({"LANG": "de_CH.UTF-8"}, "mm"),
        ({"LANG": "C"}, "mm"),
        ({}, "mm"),
    ],
)
def test_default_unit(environ, unit):
    assert default_unit(environ) == unit


@pytest.mark.parametrize(
    "setting, environ, expected",
    [
        ("mm", {"LANG": "en_US"}, MILLIMETRES),
        ("in", {"LANG": "de_DE"}, INCHES),
        ("auto", {"LANG": "en_US"}, INCHES),
        (None, {"LANG": "de_DE"}, MILLIMETRES),
        ("bogus", {"LANG": "en_US"}, INCHES),
    ],
)
def test_unit_for(setting, environ, expected):
    assert unit_for(setting, environ) is expected


def test_conversion():
    assert INCHES.to_display(25.4) == 1.0
    assert INCHES.to_display(10) == 0.39  # the digits the spin button shows
    assert INCHES.to_display(10, 3) == 0.394
    assert INCHES.from_display(2) == pytest.approx(50.8)
    assert MILLIMETRES.to_display(12.345) == 12.3
    assert MILLIMETRES.to_display(15.0, 0) == 15  # 15 mm stays 15 mm
    assert MILLIMETRES.to_display(14.6, 0) == 15
    assert MILLIMETRES.from_display(7.5) == 7.5


@pytest.mark.parametrize("mm_step, expected", [(0.1, 0.01), (0.5, 0.01), (1, 0.05), (5, 0.1)])
def test_step(mm_step, expected):
    assert INCHES.step(mm_step) == expected
    assert MILLIMETRES.step(mm_step) == mm_step


def test_format():
    assert MILLIMETRES.format(12.34) == "12.3 mm"
    assert INCHES.format(25.4) == "1.00 in"
    # like on the cassette box: metres
    assert MILLIMETRES.format_long(5000) == "5 m"
    assert MILLIMETRES.format_long(4100) == "4.1 m"
    assert MILLIMETRES.format_long(4149) == "4.1 m"
    assert MILLIMETRES.format_long(380) == "0.4 m"
    assert MILLIMETRES.format_long(4999.6) == "5 m"
    assert INCHES.format_long(5000) == "16.4 ft"


@pytest.mark.parametrize(
    "width, inch_name",
    [(9, "3/8″"), (12, "1/2″"), (19, "3/4″"), (25, "1″"), (50, "2″"), (38, "1.50″")],
)
def test_tape_name(width, inch_name):
    assert INCHES.tape_name(width) == inch_name
    assert MILLIMETRES.tape_name(width) == "%d mm" % width


def test_label_size():
    assert MILLIMETRES.label_size(25, 60.4) == "25 × 60 mm"
    assert INCHES.label_size(25, 50.8) == "1″ × 2.00″"


def test_die_cut_sizes():
    assert MILLIMETRES.size(50, 25) == "50 × 25 mm"
    assert MILLIMETRES.size(101.6, 50.8) == "101.6 × 50.8 mm"
    assert INCHES.size(101.6, 50.8) == "4.00 × 2.00″"
    # tapes keep their names, other widths get decimals
    assert MILLIMETRES.tape_name(50.8) == "50.8 mm"
    assert INCHES.tape_name(25.0) == "1″"
    assert INCHES.tape_name(25.4) == "1.00″"
    assert MILLIMETRES.label_size(50.8, 60) == "50.8 × 60 mm"
