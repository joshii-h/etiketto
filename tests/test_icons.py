"""The app icon: SVG Tiny 1.2 that QtSvg (KDE) draws, and tools/icons.py's output.

QtSvg renders SVG Tiny 1.2 only: no filters, masks, clip paths, CSS, patterns,
images, embedded fonts or text. An icon using them shows up broken or empty
in KDE's menus and task bar, so every source SVG keeps to plain shapes, solid
fills and linear or radial gradients.
"""

from __future__ import annotations

import importlib.util
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "xyz.joshuahirsig.Tavletta"
ICONS = {
    ROOT / "data" / f"{APP_ID}.svg": 128,
    ROOT / "data" / f"{APP_ID}-symbolic.svg": 16,
    ROOT / "data" / "icons" / "tavletta-16.svg": 16,
    ROOT / "data" / "icons" / "tavletta-20.svg": 20,
    ROOT / "data" / "icons" / "tavletta-24.svg": 24,
    ROOT / "data" / "icons" / "tavletta-32.svg": 32,
    ROOT / "data" / "icons" / "tavletta-macos.svg": 1024,
}
SVG_NS = "{http://www.w3.org/2000/svg}"
ALLOWED_ELEMENTS = {
    "svg",
    "defs",
    "g",
    "path",
    "rect",
    "circle",
    "ellipse",
    "line",
    "polyline",
    "polygon",
    "linearGradient",
    "radialGradient",
    "stop",
    "title",
    "desc",
}
# attributes that are not Tiny 1.2 or pull in what QtSvg cannot draw
FORBIDDEN_ATTRIBUTES = {
    "style",
    "class",
    "filter",
    "mask",
    "clip-path",
    "opacity",  # group opacity; fill-opacity and stroke-opacity are Tiny 1.2
    "gradientTransform",
    "href",
    "font-family",
}
# path commands of SVG Tiny 1.2: no elliptical arcs (A)
PATH_COMMANDS = set("MmLlHhVvCcSsQqTtZz")


def local(name: str) -> str:
    return name.split("}", 1)[-1]


@pytest.mark.parametrize("path", list(ICONS), ids=lambda p: p.name)
def test_icon_is_svg_tiny(path):
    text = path.read_text(encoding="utf-8")
    assert "@import" not in text
    assert "<!ENTITY" not in text and "<?xml-stylesheet" not in text
    root = ET.fromstring(text)  # noqa: S314 - our own files
    for element in root.iter():
        assert element.tag.startswith(SVG_NS), element.tag  # no foreign namespaces
        name = local(element.tag)
        assert name in ALLOWED_ELEMENTS, "%s: <%s>" % (path.name, name)
        for attribute, value in element.attrib.items():
            assert local(attribute) not in FORBIDDEN_ATTRIBUTES, "%s: %s" % (path.name, attribute)
            assert "url(#" in value or "url(" not in value, value  # only local gradients
        if name == "path":
            letters = set(re.findall(r"[A-Za-z]", element.get("d", "")))
            assert letters <= PATH_COMMANDS, "%s: %s" % (path.name, letters - PATH_COMMANDS)


@pytest.mark.parametrize(("path", "size"), list(ICONS.items()), ids=[p.name for p in ICONS])
def test_icon_size(path, size):
    root = ET.fromstring(path.read_text(encoding="utf-8"))  # noqa: S314
    assert root.get("viewBox") == "0 0 %d %d" % (size, size)
    assert root.get("width") == root.get("height") == str(size)


def test_gradients_are_defined_where_used():
    for path in ICONS:
        text = path.read_text(encoding="utf-8")
        for ref in re.findall(r"url\(#([^)]+)\)", text):
            assert 'id="%s"' % ref in text, (path.name, ref)


def test_symbolic_icon_is_one_colour_without_strokes():
    """GNOME recolours a symbolic icon: one fill colour, shapes only."""
    root = ET.fromstring((ROOT / "data" / f"{APP_ID}-symbolic.svg").read_text(encoding="utf-8"))  # noqa: S314
    fills = {e.get("fill") for e in root.iter() if e.get("fill")}
    assert fills == {"#2e3436"}
    assert not [e for e in root.iter() if e.get("stroke") or local(e.tag).endswith("Gradient")]


def test_no_swiss_cross():
    """The Swiss cross is protected (Wappenschutzgesetz): no red on the icon."""
    for path in ICONS:
        text = path.read_text(encoding="utf-8").lower()
        for red in ("#e01b24", "#c01c28", "#ed333b", "#a51d2d", "#ff0000"):
            assert red not in text, (path.name, red)


def load_tool():
    spec = importlib.util.spec_from_file_location("icons_tool", ROOT / "tools" / "icons.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tool_writes_png_ico_and_icns(tmp_path):
    tool = load_tool()
    written = tool.build(tmp_path)
    assert all(p.is_file() for p in written)
    for size in tool.HICOLOR:
        png = tmp_path / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        with Image.open(png) as image:
            assert image.size == (size, size) and image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0  # transparent corner
            assert image.getpixel((size // 2, size // 2))[3] == 255  # the tablet
    with Image.open(tmp_path / "tavletta.ico") as ico:
        assert sorted(s[0] for s in ico.info["sizes"]) == sorted(tool.ICO)
        ico.size = (16, 16)
        ico.load()
        assert ico.getpixel((8, 8))[3] == 255
    data = (tmp_path / "tavletta.ico").read_bytes()
    assert data.count(b"\x89PNG") == 1  # only 256 px as PNG
    with Image.open(tmp_path / "tavletta.icns") as icns:
        assert {(w, scale) for w, _h, scale in icns.info["sizes"]} == {
            (16, 1),
            (16, 2),
            (32, 1),
            (32, 2),
            (128, 1),
            (128, 2),
            (256, 1),
            (256, 2),
            (512, 1),
            (512, 2),
        }
        icns.load()
        assert icns.size == (1024, 1024)


def test_small_sizes_use_their_own_drawing():
    tool = load_tool()
    assert tool.source(16).name == "tavletta-16.svg"
    assert tool.source(20).name == "tavletta-20.svg"
    assert tool.source(24).name == "tavletta-24.svg"
    assert tool.source(32).name == "tavletta-32.svg"
    assert tool.source(40).name == "tavletta-32.svg"
    assert tool.source(48).name == f"{APP_ID}.svg"


def test_packaging_installs_the_small_drawings():
    """setup.py installs the small drawings as the hicolor 16-32 px icons;
    the sdist and install.sh carry them along."""
    setup = (ROOT / "setup.py").read_text(encoding="utf-8")
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert "data/icons/tavletta-{size}.svg" in setup
    for size in (16, 24, 32):
        assert (ROOT / "data" / "icons" / f"tavletta-{size}.svg").is_file()
        assert f"{size}x{size}" in install
    assert "include data/icons/tavletta-*.svg" in manifest
