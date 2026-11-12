"""The app icon: SVG that QtSvg (KDE) draws, and what is built from it.

QtSvg renders SVG Tiny 1.2 only: no filters, masks, clip paths, CSS, patterns,
images, embedded fonts or text. An icon using them shows up broken or empty
in KDE's menus and task bar, so every source SVG keeps to plain shapes, solid
fills and linear or radial gradients. A few rules here go further than Tiny
1.2 (house rules): colours only as #rrggbb, no group opacity, no gradient
transforms, no attributes from other namespaces.

The tests that render (tools/icons.py) need librsvg's GObject introspection.
Without it they are skipped, except in the CI (the variable CI is set),
where a missing librsvg is a failure.
"""

from __future__ import annotations

import colorsys
import importlib.util
import io
import os
import re
import struct
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
APP_ID = "xyz.joshuahirsig.Tavletta"
MAIN = ROOT / "data" / f"{APP_ID}.svg"
SYMBOLIC = ROOT / "data" / f"{APP_ID}-symbolic.svg"
SVGS = [MAIN, SYMBOLIC, *sorted((ROOT / "data" / "icons").glob("*.svg"))]
SVG_NS = "{http://www.w3.org/2000/svg}"
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

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
FORBIDDEN_ATTRIBUTES = {
    # not in Tiny 1.2, or pulling in what QtSvg cannot draw
    "style",
    "class",
    "filter",
    "mask",
    "clip-path",
    "href",
    "font-family",
    # house rules: Tiny 1.2 has them, but they are easy to get wrong
    "opacity",  # group opacity; fill-opacity and stroke-opacity are fine
    "gradientTransform",
}
COLOUR_ATTRIBUTES = {"fill", "stroke", "stop-color", "color"}
HEX_COLOUR = re.compile(r"#[0-9a-f]{6}")
LOCAL_URL = re.compile(r"url\(#([A-Za-z_][\w.-]*)\)")
# path commands of SVG Tiny 1.2 (no elliptical arcs) and its numbers,
# exponents included (1e-3, 2.5E+2)
PATH_COMMANDS = set("MmLlHhVvCcSsQqTtZz")
NUMBER = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")


def local(name: str) -> str:
    return name.split("}", 1)[-1]


def parse(path: Path) -> ET.Element:
    return ET.fromstring(path.read_text(encoding="utf-8"))  # noqa: S314 - our own files


def expected_size(path: Path) -> int:
    if path == MAIN:
        return 128
    if path == SYMBOLIC:
        return 16
    if path.stem == "tavletta-macos":
        return 1024
    match = re.fullmatch(r"tavletta-(?:macos-)?(\d+)", path.stem)
    assert match, "%s: name says no size" % path.name
    return int(match.group(1))


def path_commands(d: str) -> set[str]:
    """The letters of path data, with the numbers (and their exponents) removed."""
    rest = NUMBER.sub(" ", d)
    assert re.fullmatch(r"[A-Za-z\s,]*", rest), rest
    return set(re.findall(r"[A-Za-z]", rest))


def test_path_commands_read_exponents_as_numbers():
    assert path_commands("M1e-3 2E+2L.5.5h1e2z") == {"M", "L", "h", "z"}
    assert path_commands("M0 0a4 4 0 0 1 4 4") == {"M", "a"}


def test_every_icon_file_is_checked():
    assert len(SVGS) == len(set(SVGS)) >= 9
    assert {p.name for p in SVGS} >= {
        f"{APP_ID}.svg",
        "tavletta-16.svg",
        "tavletta-22.svg",
        "tavletta-macos.svg",
        "tavletta-macos-16.svg",
    }


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_icon_keeps_to_svg_tiny(path):
    text = path.read_text(encoding="utf-8")
    assert "@import" not in text
    assert "<!ENTITY" not in text and "<?xml-stylesheet" not in text
    for element in parse(path).iter():
        assert element.tag.startswith(SVG_NS), element.tag  # no foreign elements
        name = local(element.tag)
        assert name in ALLOWED_ELEMENTS, "%s: <%s>" % (path.name, name)
        for attribute, value in element.attrib.items():
            # Inkscape's, sodipodi's, xlink's ... attributes have a namespace
            assert not attribute.startswith("{"), "%s: %s" % (path.name, attribute)
            assert attribute not in FORBIDDEN_ATTRIBUTES, "%s: %s" % (path.name, attribute)
            if "url(" in value:
                assert LOCAL_URL.fullmatch(value), "%s: %s" % (path.name, value)
            if attribute in COLOUR_ATTRIBUTES:
                ok = value == "none" or HEX_COLOUR.fullmatch(value) or LOCAL_URL.fullmatch(value)
                if path == SYMBOLIC and value == "currentColor":
                    ok = True
                assert ok, "%s: %s=%s" % (path.name, attribute, value)
        if name == "path":
            commands = path_commands(element.get("d", ""))
            assert commands <= PATH_COMMANDS, "%s: %s" % (path.name, commands - PATH_COMMANDS)


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_icon_size(path):
    root = parse(path)
    size = expected_size(path)
    assert root.get("viewBox") == "0 0 %d %d" % (size, size)
    assert root.get("width") == root.get("height") == str(size)


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_gradients_are_defined_where_used(path):
    root = parse(path)
    gradients = {
        e.get("id") for e in root.iter() if local(e.tag) in ("linearGradient", "radialGradient")
    }
    for element in root.iter():
        for value in element.attrib.values():
            for ref in LOCAL_URL.findall(value):
                assert ref in gradients, (path.name, ref)


def test_symbolic_icon_is_one_colour_without_strokes():
    """GNOME recolours a symbolic icon: one fill colour, shapes only."""
    root = parse(SYMBOLIC)
    fills = {e.get("fill") for e in root.iter() if e.get("fill")}
    assert fills == {"#2e3436"}
    assert not [e for e in root.iter() if e.get("stroke") or local(e.tag).endswith("Gradient")]


# --- no red (no Swiss cross, which is protected: Wappenschutzgesetz) ----------

NAMED_COLOURS = {
    "red": "#ff0000",
    "darkred": "#8b0000",
    "crimson": "#dc143c",
    "firebrick": "#b22222",
    "indianred": "#cd5c5c",
    "orangered": "#ff4500",
    "tomato": "#ff6347",
    "maroon": "#800000",
}


def colours(text: str) -> list[tuple[float, float, float]]:
    """Every colour in an SVG's text as RGB (0-1): #rgb, #rrggbb, rgb(), rgba(), names."""
    found = []
    text = text.lower()
    for hex_ in re.findall(r"#([0-9a-f]{6}|[0-9a-f]{3})\b", text):
        if len(hex_) == 3:
            hex_ = "".join(c * 2 for c in hex_)
        found.append(tuple(int(hex_[i : i + 2], 16) / 255 for i in (0, 2, 4)))
    for args in re.findall(r"rgba?\(([^)]*)\)", text):
        parts = [p.strip() for p in re.split(r"[,\s/]+", args.strip()) if p.strip()][:3]
        found.append(
            tuple(float(p[:-1]) / 100 if p.endswith("%") else float(p) / 255 for p in parts)
        )
    for name, hex_ in NAMED_COLOURS.items():
        if re.search(r"(?<![\w-])%s(?![\w-])" % name, text):
            found.append(tuple(int(hex_[i : i + 2], 16) / 255 for i in (1, 3, 5)))
    return found


def is_red(rgb: tuple[float, float, float]) -> bool:
    hue, saturation, value = colorsys.rgb_to_hsv(*rgb)
    return (hue < 15 / 360 or hue > 345 / 360) and saturation > 0.4 and value > 0.3


def test_red_is_found_in_every_notation():
    for text in ('fill="#f00"', 'fill="#E01B24"', 'fill="red"', "fill='rgb(224, 27, 36)'"):
        assert any(map(is_red, colours(text))), text
    assert any(map(is_red, colours("stop-color:rgba(100%,0%,0%,0.5)")))
    # the icon's wood, the label's blue and the greys are not red
    for text in ("#865e3c #b5835a #cdab8f #c09068 #3584e4 #241f31 #c0bfbc #ffffff", "fill-rule"):
        assert not any(map(is_red, colours(text))), text


@pytest.mark.parametrize("path", SVGS, ids=lambda p: p.name)
def test_no_red(path):
    reds = [c for c in colours(path.read_text(encoding="utf-8")) if is_red(c)]
    assert not reds, (path.name, reds)


# --- the macOS icon is generated from the main icon ---------------------------


def load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_macos_icon_is_up_to_date():
    """data/icons/tavletta-macos.svg is what tools/macos_icon.py makes of the main icon."""
    macos_icon = load("macos_icon")
    assert (ROOT / "data" / "icons" / "tavletta-macos.svg").read_text(
        encoding="utf-8"
    ) == macos_icon.svg(), "run python3 tools/macos_icon.py"
    macos = macos_icon.svg()
    main = MAIN.read_text(encoding="utf-8")
    shapes = re.findall(r"<(?:rect|path|circle)\b[^>]*/>", main.split("</defs>", 1)[1])
    stops = re.findall(r"<stop\b[^>]*/>", main)
    assert shapes and stops
    for line in shapes + stops:  # the tablet, with its gradients
        assert line in macos, line


# --- tools/icons.py ------------------------------------------------------------


@pytest.fixture(scope="module")
def tool():
    try:
        import gi

        gi.require_version("Rsvg", "2.0")
        from gi.repository import Rsvg  # noqa: F401
    except (ImportError, ValueError) as error:
        if os.environ.get("CI"):
            pytest.fail("librsvg's GObject introspection is missing: %s" % error)
        pytest.skip("librsvg's GObject introspection (Rsvg 2.0) is not installed")
    return load("icons")


@pytest.fixture(scope="module")
def built(tool, tmp_path_factory):
    out = tmp_path_factory.mktemp("icons")
    return tool, out, tool.build(out)


def test_sizes_use_a_sharp_drawing(tool):
    small = {n: "tavletta-%d.svg" % n for n in (16, 20, 22, 24, 32)}
    for size, name in small.items():
        assert tool.source(size).name == name
    # twice a small drawing: whole pixels
    assert tool.source(40).name == "tavletta-20.svg"
    assert tool.source(48).name == "tavletta-24.svg"
    for size in (64, 96, 128, 512):
        assert tool.source(size) == MAIN
    for size in (18, 30, 56):
        with pytest.raises(ValueError):
            tool.source(size)
    assert tool.macos_source(16).name == "tavletta-macos-16.svg"
    assert tool.macos_source(32).name == "tavletta-macos-32.svg"
    assert tool.macos_source(64).name == "tavletta-macos.svg"


def test_hicolor_pngs(built):
    tool, out, written = built
    assert all(p.is_file() for p in written)
    for size in tool.HICOLOR:
        png = out / "hicolor" / f"{size}x{size}" / "apps" / f"{APP_ID}.png"
        with Image.open(png) as image:
            assert image.size == (size, size) and image.mode == "RGBA"
            assert image.getpixel((0, 0))[3] == 0  # transparent corner
            assert image.getpixel((size // 2, size // 2))[3] == 255  # the tablet


def ico_entries(data: bytes) -> list[tuple[int, bytes]]:
    """(size, payload) of each ICO directory entry."""
    reserved, kind, count = struct.unpack_from("<HHH", data)
    assert (reserved, kind) == (0, 1)
    entries = []
    for i in range(count):
        width, height, _colours, _r, planes, bits, length, offset = struct.unpack_from(
            "<BBBBHHII", data, 6 + 16 * i
        )
        assert width == height and planes == 1 and bits == 32
        entries.append((width or 256, data[offset : offset + length]))
    return entries


def test_ico(built):
    tool, out, _written = built
    entries = ico_entries((out / "tavletta.ico").read_bytes())
    assert [size for size, _ in entries] == sorted(tool.ICO)
    for size, payload in entries:
        if size >= 256:
            assert payload.startswith(PNG_MAGIC)
            with Image.open(io.BytesIO(payload)) as image:
                assert image.size == (size, size)
            continue
        header, width, height, planes, bits = struct.unpack_from("<IiiHH", payload)
        assert (header, width, height, planes, bits) == (40, size, 2 * size, 1, 32)
        stride = ((size + 31) // 32) * 4
        assert len(payload) == 40 + 4 * size * size + stride * size
        mask = payload[40 + 4 * size * size :]
        image = tool.render(tool.source(size), size)
        for x, y in ((0, 0), (size // 2, size // 2)):
            row = size - 1 - y  # bottom up
            bit = mask[row * stride + x // 8] & (0x80 >> (x % 8))
            assert bool(bit) == (image.getpixel((x, y))[3] == 0), (size, x, y)
    with Image.open(out / "tavletta.ico") as ico:
        assert sorted(s[0] for s in ico.info["sizes"]) == sorted(tool.ICO)


def test_ico_refuses_sizes_above_256(tool):
    with pytest.raises(ValueError, match="256"):
        tool.ico({512: Image.new("RGBA", (512, 512))})


def rle_decode(data: bytes, length: int) -> tuple[bytes, int]:
    """Apple's icns run-length encoding; returns the bytes and how many were read."""
    out = bytearray()
    i = 0
    while len(out) < length:
        control = data[i]
        if control < 0x80:
            out += data[i + 1 : i + 2 + control]
            i += 2 + control
        else:
            out += bytes([data[i + 1]]) * (control - 0x80 + 3)
            i += 2
    assert len(out) == length
    return bytes(out), i


def test_icns_rle_round_trip(tool):
    samples = [
        b"",
        b"a",
        b"ab",
        b"aab",
        bytes(300),
        bytes(range(256)) * 2,
        b"x" * 130 + b"y" * 131 + b"z" * 2 + bytes(range(200)),
    ]
    for sample in samples:
        encoded = tool.icns_rle(sample)
        assert rle_decode(encoded, len(sample)) == (sample, len(encoded))


def icns_entries(data: bytes) -> dict[bytes, bytes]:
    assert data[:4] == b"icns" and struct.unpack(">I", data[4:8])[0] == len(data)
    entries = {}
    i = 8
    while i < len(data):
        kind, length = data[i : i + 4], struct.unpack(">I", data[i + 4 : i + 8])[0]
        entries[kind] = data[i + 8 : i + length]
        i += length
    return entries


def test_icns(built):
    tool, out, _written = built
    data = (out / "tavletta.icns").read_bytes()
    entries = icns_entries(data)
    # no icp4/icp5: macOS 10.15.7+ draws PNG at 16 and 32 px 1x scrambled
    assert set(entries) == {
        *tool.ICNS_PNG,
        b"ic04",
        b"ic05",
        b"is32",
        b"s8mk",
        b"il32",
        b"l8mk",
    }
    for kind, size in tool.ICNS_PNG.items():
        assert entries[kind].startswith(PNG_MAGIC), kind
        with Image.open(io.BytesIO(entries[kind])) as image:
            assert image.size == (size, size), kind
    for kind, size in (("ic04", 16), ("ic05", 32)):
        payload = entries[kind.encode()]
        assert payload[:4] == b"ARGB"
        pos, channels = 4, []
        for _ in range(4):
            channel, used = rle_decode(payload[pos:], size * size)
            channels.append(channel)
            pos += used
        assert pos == len(payload)
        expected = tool.render(tool.macos_source(size), size)
        assert channels == [expected.getchannel(c).tobytes() for c in "ARGB"], kind
    with Image.open(out / "tavletta.icns") as icns:
        assert {(w, scale) for w, _h, scale in icns.info["sizes"]} >= {(16, 1), (32, 1)}
        for size in (16, 32):  # is32/il32 with their masks, as Pillow reads them
            image = icns.icns.getimage((size, size, 1))
            expected = tool.render(tool.macos_source(size), size)
            assert image.tobytes() == expected.tobytes(), size
        icns.load()
        assert icns.size == (1024, 1024)


# --- packaging -----------------------------------------------------------------


def small_icon_sizes() -> tuple[int, ...]:
    setup = (ROOT / "setup.py").read_text(encoding="utf-8")
    match = re.search(r"^SMALL_ICONS = \(([\d, ]+)\)$", setup, re.M)
    assert match
    return tuple(int(n) for n in match.group(1).split(","))


def test_setup_installs_the_small_drawings(tmp_path):
    """A real install of the data files: each small drawing as its hicolor icon."""
    built = subprocess.run(
        [
            sys.executable,
            "setup.py",
            "-q",
            "build",
            "--build-base",
            str(tmp_path / "build"),
            "install_data",
            "--install-dir",
            str(tmp_path / "root"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stderr  # the error itself, not just its exit status
    hicolor = tmp_path / "root" / "share" / "icons" / "hicolor"
    sizes = small_icon_sizes()
    assert sizes == (16, 20, 22, 24, 32)
    for size in sizes:
        installed = hicolor / f"{size}x{size}" / "apps" / f"{APP_ID}.svg"
        source = ROOT / "data" / "icons" / f"tavletta-{size}.svg"
        assert installed.read_bytes() == source.read_bytes(), size
    assert (hicolor / "scalable" / "apps" / f"{APP_ID}.svg").read_bytes() == MAIN.read_bytes()
    assert (hicolor / "symbolic" / "apps" / f"{APP_ID}-symbolic.svg").is_file()


def test_install_sh_installs_and_removes_the_small_drawings():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    sizes = re.search(r'^SMALL_ICON_SIZES="([^"]+)"$', install, re.M)
    assert sizes
    assert sizes.group(1).split() == ["%dx%d" % (n, n) for n in small_icon_sizes()]
    uninstall, rest = install.split('if [ "${1:-}" = "--uninstall" ]; then', 1)[1].split(
        "    exit 0\n", 1
    )
    loop = "for size in $SMALL_ICON_SIZES; do"
    target = '"$DATA/icons/hicolor/$size/apps/$APP_ID.svg"'
    removing = uninstall.split(loop, 1)[1].split("done", 1)[0]
    assert "rm -f %s" % target in removing
    assert 'rmdir "$DATA/icons/hicolor/$size/apps" "$DATA/icons/hicolor/$size"' in removing
    installing = rest.split(loop, 1)[1].split("done", 1)[0]
    assert 'mkdir -p "$DATA/icons/hicolor/$size/apps"' in installing
    assert (
        'cp "$SHARE/icons/hicolor/$size/apps/$APP_ID.svg" "$DATA/icons/hicolor/$size/apps/"'
        in (installing)
    )


def test_the_sdist_carries_every_icon():
    manifest = (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert "include data/icons/tavletta-*.svg" in manifest
    assert all(p.name.startswith("tavletta-") for p in (ROOT / "data" / "icons").glob("*.svg"))


# --- site and workflows --------------------------------------------------------


def test_site_renders_the_png_icons(tool, tmp_path):
    """site/build.py: a 32 px PNG favicon and an opaque 180 px iOS icon, linked
    from every page (overrides/main.html) and from the language landing page."""
    spec = importlib.util.spec_from_file_location("site_build", ROOT / "site" / "build.py")
    assert spec and spec.loader
    build = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build)
    build.render_png(build.SMALL_ICON, 32, tmp_path / "favicon-32.png")
    build.render_png(build.ICON, 180, tmp_path / "touch.png", build.TOUCH_BACKGROUND)
    with Image.open(tmp_path / "favicon-32.png") as image:
        assert image.size == (32, 32)
        assert image.convert("RGBA").getpixel((0, 0))[3] == 0
    with Image.open(tmp_path / "touch.png") as image:
        assert image.size == (180, 180)
        assert image.convert("RGBA").getpixel((0, 0)) == (0xF6, 0xF5, 0xF4, 255)
    main = (ROOT / "site" / "overrides" / "main.html").read_text(encoding="utf-8")
    source = (ROOT / "site" / "build.py").read_text(encoding="utf-8")
    for name in ("favicon-32.png", "favicon.svg", "apple-touch-icon.png"):
        assert "assets/%s" % name in main, name
        assert "en/assets/%s" % name in source, name  # the landing page
        assert 'docs / "assets" / "%s"' % name in source, name  # written by stage()
    assert '{% extends "base.html" %}' in main and "{% block extrahead %}" in main


def test_workflows_install_librsvg_and_watch_the_icons():
    workflows = ROOT / ".forgejo" / "workflows"
    ci = (workflows / "ci.yml").read_text(encoding="utf-8")
    assert "gir1.2-rsvg-2.0" in ci and 'CI: "true"' in ci
    for name in ("docs.yml", "docs-check.yml"):
        assert "gir1.2-rsvg-2.0" in (workflows / name).read_text(encoding="utf-8"), name
    check = (workflows / "docs-check.yml").read_text(encoding="utf-8")
    assert "python3-gi-cairo" in check and "venv --system-site-packages" in check
    filters = re.findall(r"^\s+paths: \[(.*)\]$", check, re.M)
    assert len(filters) == 2
    for line in filters:
        assert '"data/*.svg"' in line and '"data/icons/**"' in line
