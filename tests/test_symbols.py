"""The symbol library: the bundled icon set, its search, the list of
recently used symbols, the converter in tools/ and the packaging."""

from __future__ import annotations

import fnmatch
import gzip
import importlib.util
import json
import tomllib
from pathlib import Path

import pytest

from etiketto import symbols
from etiketto.symbols import path as symbol_path

ROOT = Path(__file__).resolve().parent.parent


def names(found):
    return [s.name for s in found]


# --- the icon set -------------------------------------------------------------------


def test_the_icon_set_is_complete_and_without_brands():
    everything = symbols.all_symbols()
    assert symbols.source() == "Material Design Icons 7.4.47"
    assert len(everything) == 7160
    assert names(everything) == sorted(set(names(everything)))  # sorted, each once
    cats = symbols.categories()
    assert list(cats) == sorted(cats) and "Brand / Logo" not in cats
    assert "Social Media" not in cats  # it only had brands
    assert symbols.find("facebook") is None and symbols.find("android") is None
    printer = symbols.find("printer")
    assert printer and printer.categories == ("Home Automation", "Printer")
    assert printer.path.startswith("M18,3H6V7H18")
    assert symbols.find("account").aliases[:2] == ("person", "user")
    assert symbols.find("no-such-symbol") is None


def test_brands_and_trademarks_are_left_out(tool):
    """Beyond "Brand / Logo": the deny-list of the converter, and no alias
    names a brand; standard marks of ports and features stay."""
    everything = names(symbols.all_symbols())
    for pattern in tool.DENIED:
        assert not fnmatch.filter(everything, pattern), pattern
    for name in ("raspberry-pi", "microsoft-xbox-controller-battery-unknown", "pokeball"):
        assert symbols.find(name) is None
    for symbol in symbols.all_symbols():
        for alias in symbol.aliases:
            assert not tool.BRAND_WORDS & set(alias.split("-")), (symbol.name, alias)
    assert symbols.find("toy-brick").aliases == ("extension", "plugin")  # no "lego"
    assert symbols.find("ghost").aliases == ()  # Pac-Man's ghosts
    for name in ("bluetooth", "usb", "nfc-variant", "cast", "contactless-payment", "hdmi-port"):
        assert symbols.find(name), name
    assert symbols.find("apple-keyboard-command")  # a key symbol, not a logo


def test_every_symbol_parses_inside_its_square():
    for symbol in symbols.all_symbols():
        # the points the path runs through (control points may lie outside)
        points = [v for s in symbol_path.parse(symbol.path) for v in s[-2:] if s[0] != "Z"]
        assert all(-0.01 <= v <= 24.01 for v in points), symbol.name


# --- search ----------------------------------------------------------------------------


def test_search_ranks_the_name_first():
    found = names(symbols.search("printer"))
    assert found[0] == "printer"
    assert found[1].startswith("printer-")  # names starting with the query next
    # a name that adds words to the query, not one that only starts with it
    assert names(symbols.search("print"))[0] == "printer"
    assert "printer" not in names(symbols.search("print"))[1:]
    found = names(symbols.search("Arrow Left"))
    assert found[:3] == ["arrow-left", "arrow-left-bold", "arrow-left-bottom"]


def test_search_finds_aliases_and_categories():
    # "user" is an alias of account
    assert "account" in names(symbols.search("user"))
    hospital = symbols.search("medical")
    assert "hospital-box" in names(hospital)  # by its category "Medical / Hospital"
    for symbol in hospital:
        name, aliases, cats = symbol.words
        assert any(word.startswith("medical") for word in name | aliases | cats)


def test_search_needs_every_word():
    found = names(symbols.search("arrow bold left"))
    assert found[0] == "arrow-left-bold"  # every word in the name, the shortest name
    assert "arrow-left-bold" in found and "arrow-right-bold" not in found
    assert symbols.search("zzzz nothing") == []


def test_search_with_words_written_together_or_apart():
    for query in ("wi-fi", "wi fi", "WiFi"):
        assert names(symbols.search(query))[0] == "wifi", query
    assert names(symbols.search("e mail"))[0] == "email"
    assert names(symbols.search("arrowleft")) == ["arrow-left"]
    assert symbols.search("--") == [] and symbols.search("!?") == []  # nothing to look for


def test_search_within_a_category():
    printers = symbols.search("", "Printer")
    assert len(printers) > 40 and all("Printer" in s.categories for s in printers)
    assert names(symbols.search("3d", "Printer"))[0] == "printer-3d"
    assert symbols.search("", None) == symbols.all_symbols()
    assert symbols.search("  ") == symbols.all_symbols()


def test_remember_recently_used():
    assert symbols.remember(None, "star") == ["star"]
    assert symbols.remember(["a", 3, "star", "b"], "star") == ["star", "a", "b"]
    many = ["n%d" % n for n in range(40)]
    assert symbols.remember(many, "x") == ["x", *many[: symbols.MAX_RECENT - 1]]
    assert symbols.remember({"bad": 1}, "x") == ["x"]


def test_segments_are_cached_and_traced():
    path = "M1,2L3,4C1,1 2,2 3,3Z"
    assert symbols.segments(path) is symbols.segments(path)
    calls = []

    class Ctx:
        def __getattr__(self, name):
            return lambda *args: calls.append((name, args))

    symbols.trace(Ctx(), symbols.segments(path))
    assert calls == [
        ("move_to", (1, 2)),
        ("line_to", (3, 4)),
        ("curve_to", (1, 1, 2, 2, 3, 3)),
        ("close_path", ()),
    ]
    with pytest.raises(symbols.PathError):
        symbols.segments("L1,1")
    hits = symbols._parsed.cache_info().hits
    with pytest.raises(symbols.PathError, match="start with M"):
        symbols.segments("L1,1")  # the failure is cached too
    assert symbols._parsed.cache_info().hits == hits + 1


# --- the converter (tools/mdi_symbols.py) ---------------------------------------------


@pytest.fixture
def tool(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("mdi_symbols", ROOT / "tools" / "mdi_symbols.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    out = tmp_path / "out"
    out.mkdir()
    (out / "LICENSE.mdi").write_text("header\nUpstream license text\n\nApache")
    monkeypatch.setattr(module, "OUT", out)
    return module


def _package(folder: Path, license_text="Upstream license text") -> Path:
    icons = [
        ("zeta", ["Arrow"], ["z-alias"], False, "M0,0H24V24Z"),
        ("alpha", ["Shape", "Arrow"], ["b", "a"], False, "M1,1L2,2Z"),
        ("old", ["Arrow"], [], True, "M0,0Z"),
        ("brand", ["Brand / Logo", "Social Media"], [], False, "M0,0Z"),
        ("pokeball", ["Gaming / RPG"], [], False, "M0,0Z"),  # on the deny-list
        ("nintendo-game-boy", ["Arrow"], [], False, "M0,0Z"),
        ("brick", ["Shape"], ["lego", "plugin", "lego-outline"], False, "M2,2Z"),
    ]
    (folder / "svg").mkdir(parents=True)
    meta = []
    for name, tags, aliases, deprecated, d in icons:
        meta.append({"name": name, "tags": tags, "aliases": aliases, "deprecated": deprecated})
        (folder / "svg" / ("%s.svg" % name)).write_text(
            '<svg xmlns="http://www.w3.org/2000/svg" id="mdi-%s" viewBox="0 0 24 24">'
            '<path d="%s" /></svg>' % (name, d)
        )
    (folder / "meta.json").write_text(json.dumps(meta))
    (folder / "package.json").write_text(json.dumps({"version": "1.2.3"}))
    (folder / "LICENSE").write_text(license_text + "\n")
    return folder


def test_the_converter_keeps_what_etiketto_needs(tool, tmp_path, capsys):
    package = _package(tmp_path / "pkg")
    assert tool.main(["mdi_symbols.py", str(package)]) == 0
    target = tool.OUT / "mdi.json.gz"
    first = target.read_bytes()
    assert first[4:8] == b"\0\0\0\0"  # no timestamp in the gzip header
    data = json.loads(gzip.decompress(first))
    assert data == {
        "set": "Material Design Icons",
        "version": "1.2.3",
        # no brands, nothing only deprecated or denied icons had
        "categories": ["Arrow", "Shape"],
        "icons": [
            ["alpha", "M1,1L2,2Z", [0, 1], ["a", "b"]],
            ["brick", "M2,2Z", [1], ["plugin"]],  # without the brand aliases
            ["zeta", "M0,0H24V24Z", [0], ["z-alias"]],
        ],
    }
    assert "1.2.3: 3 icons in 2 categories" in capsys.readouterr().out
    tool.main(["mdi_symbols.py", str(package)])
    assert target.read_bytes() == first  # reproducible


def test_the_converter_stops_on_a_new_license_or_odd_svg(tool, tmp_path):
    with pytest.raises(SystemExit, match="license"):
        tool.main(["mdi_symbols.py", str(_package(tmp_path / "a", "Another license"))])
    package = _package(tmp_path / "b")
    (package / "svg" / "alpha.svg").write_text('<svg viewBox="0 0 48 48"><path d="M0,0"/></svg>')
    with pytest.raises(SystemExit, match="alpha.svg"):
        tool.main(["mdi_symbols.py", str(package)])
    assert tool.main(["mdi_symbols.py"]) == 2  # usage


# --- packaging -------------------------------------------------------------------------


def test_the_icon_data_and_its_license_are_packaged():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    data = project["tool"]["setuptools"]["package-data"]["etiketto.symbols"]
    assert set(data) == {"mdi.json.gz", "LICENSE.mdi"}
    assert "etiketto/symbols/LICENSE.mdi" in project["project"]["license-files"]
    assert "Apache-2.0" in project["project"]["license"]
    for name in data:
        assert (ROOT / "etiketto" / "symbols" / name).is_file()
    license_text = (ROOT / "etiketto" / "symbols" / "LICENSE.mdi").read_text()
    assert "Pictogrammers Free License" in license_text
    assert "Apache License" in license_text and "Version 2.0, January 2004" in license_text
