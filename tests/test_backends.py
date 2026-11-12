"""The backend registry, the shared types in base.py and the contract for
the test-only mono backend."""

from types import SimpleNamespace

import pytest
from backend_contract import BackendContract
from fake_backends import FakeMono
from PIL import Image

import etiketto.backends as registry
from etiketto.backends import backends, get_backend
from etiketto.backends.base import (
    Backend,
    ColorAdjust,
    LabelTooLong,
    PrinterError,
    PrinterInfo,
    PrintMode,
    PrintOptions,
    Raster,
    apply_color_model,
)
from etiketto.backends.vc500w import VC500W
from etiketto.model import DEFAULT_PRINTER, Label


@pytest.fixture
def mono(monkeypatch):
    backend = FakeMono()
    registry.register(backend)
    yield backend
    registry.unregister(backend.id)


# --- registry ------------------------------------------------------------------


def test_default_backend_is_registered():
    backend = get_backend()
    assert backend.id == DEFAULT_PRINTER == "vc500w"
    assert isinstance(backend, VC500W)
    assert get_backend("vc500w") is backend


def test_unknown_backend():
    with pytest.raises(KeyError):
        get_backend("no-such-printer")


def test_register_orders_default_first(mono):
    # the default first, then by name, whatever the case
    # names: Brother P-touch, Brother QL (twice, in the order registered), cab,
    # CUPS / IPP, DYMO, ESC/POS, Fake Mono, NIIMBOT, TSPL, Zebra EPL2, Zebra ZPL
    ql = ["brother_ql", "brother_ql_diecut"]
    builtin = ["ptouch", *ql, "cab", "system", "dymo", "escpos"]
    assert [b.id for b in backends()] == [
        "vc500w",
        *builtin,
        "fake-mono",
        "niimbot",
        "tspl",
        "epl",
        "zpl",
    ]
    assert get_backend("fake-mono") is mono
    registry.unregister("fake-mono")
    registry.unregister("fake-mono")  # twice is fine
    assert [b.id for b in backends()] == ["vc500w", *builtin, "niimbot", "tspl", "epl", "zpl"]


def test_register_needs_an_id():
    class Nameless(FakeMono):
        id = ""

    with pytest.raises(ValueError):
        registry.register(Nameless())


def test_entry_points(monkeypatch):
    class Plugin(FakeMono):
        id = "plugin"

    entries = [
        SimpleNamespace(name="class", load=lambda: Plugin),
        SimpleNamespace(name="instance", load=lambda: FakeMono()),
        SimpleNamespace(name="broken", load=lambda: 1 / 0),
    ]
    groups = []

    def entry_points(group):
        groups.append(group)
        return entries

    monkeypatch.setattr(registry, "entry_points", entry_points)
    monkeypatch.setattr(registry, "_loaded", False)
    monkeypatch.setattr(registry, "_registry", {})
    ids = [b.id for b in backends()]
    assert groups == ["etiketto.backends"]
    assert ids[0] == "vc500w"
    assert sorted(ids[1:]) == sorted(
        [
            "brother_ql",
            "brother_ql_diecut",
            "cab",
            "fake-mono",
            "niimbot",
            "plugin",
            "ptouch",
            "system",
            "zpl",
            "epl",
            "tspl",
            "escpos",
            "dymo",
        ]
    )
    backends()  # loaded once only
    assert groups == ["etiketto.backends"]


def test_every_backend_has_a_contract_test():
    import test_brother_ql  # noqa: F401 - define their BackendContract subclasses
    import test_cab  # noqa: F401
    import test_niimbot  # noqa: F401
    import test_ptouch  # noqa: F401
    import test_raw_backends  # noqa: F401
    import test_system  # noqa: F401
    import test_vc500w  # noqa: F401

    tested = {cls.backend_id for cls in BackendContract.__subclasses__()}
    assert {b.id for b in backends()} <= tested


# --- shared types ----------------------------------------------------------------


@pytest.mark.parametrize(
    "data, expected",
    [
        (None, ColorAdjust()),
        ("junk", ColorAdjust()),
        ({}, ColorAdjust()),
        ({"brightness": 2, "red": -1}, ColorAdjust(2, -1, 0, 0)),
        ({"brightness": 9, "green": -7.4, "blue": 1.6}, ColorAdjust(3, 0, -3, 2)),
        ({"red": True, "green": "2", "blue": None}, ColorAdjust()),
    ],
)
def test_color_adjust_from_dict(data, expected):
    assert ColorAdjust.from_dict(data) == expected


def test_color_adjust_round_trip_and_neutral():
    adjust = ColorAdjust(1, -2, 3, 0)
    assert adjust.to_dict() == {"brightness": 1, "red": -2, "green": 3, "blue": 0}
    assert ColorAdjust.from_dict(adjust.to_dict()) == adjust
    assert ColorAdjust().is_neutral() and not adjust.is_neutral()
    assert not ColorAdjust(blue=-1).is_neutral()


def test_printer_error_keeps_details_and_params():
    error = LabelTooLong("17.5 in", max_mm=431.8)
    assert isinstance(error, PrinterError) and error.code == "too_long"
    assert error.details == str(error) == "17.5 in"
    assert error.params == {"max_mm": 431.8}
    assert PrinterError().code == "generic" and PrinterError().details == ""


def test_apply_color_model_dithers_mono_only():
    grey = Image.new("RGB", (40, 40), (128, 128, 128))
    dithered = apply_color_model(grey, "mono")
    colours = {c: n for n, c in dithered.getcolors()}
    assert set(colours) == {(0, 0, 0), (255, 255, 255)}
    assert abs(colours[(0, 0, 0)] - 800) < 80  # half black: Floyd-Steinberg, not a threshold
    assert apply_color_model(grey, "color") is grey
    assert apply_color_model(grey, "two-color") is grey


class Minimal(Backend):
    """Only what a backend must implement: the defaults of base.Backend."""

    id = "minimal"
    name = "Minimal"
    capabilities = FakeMono.capabilities

    def discover(self, timeout=3.0):
        return []

    def query(self, host):
        return PrinterInfo("Minimal", None, None, None, "IDLE")

    def print_label(self, image, host, options, progress=None):
        pass


def test_backend_defaults():
    backend = Minimal()
    assert backend.configured_host() is None
    assert backend.raster(12, PrintOptions()) == Raster(203)
    reported = (PrintMode("fine", 600),)
    assert backend.modes(None) == FakeMono.capabilities.modes
    assert backend.modes(PrinterInfo("m", 12, None, None, "IDLE")) == FakeMono.capabilities.modes
    info = PrinterInfo("m", 12, None, None, "IDLE", modes=reported)
    assert backend.modes(info) == reported


def test_prepare_crops_to_the_raster_width(monkeypatch):
    backend = Minimal()
    monkeypatch.setattr(
        backend, "raster", lambda tape, options: Raster(203, tape_px=90, bleed_px=5, width_px=99)
    )
    image = backend.prepare(Label(tape_mm=12, background="#000000"), PrintOptions())
    assert image.height == 99  # 90 + 2 × 5, one row cut off


# --- the contract for the test backend -------------------------------------------


class TestFakeMono(BackendContract):
    backend_id = "fake-mono"

    @pytest.fixture
    def harness(self):
        backend = FakeMono()

        def fail():
            backend.error = PrinterError("out of paper")

        return SimpleNamespace(backend=backend, fail=fail, printed=lambda: len(backend.jobs))


def test_a_code_too_large_for_the_tape_is_not_printed():
    from etiketto.backends.base import CodeTooLarge
    from etiketto.model import CodeElement, Label

    label = Label(tape_mm=9, elements=[CodeElement("x" * 1000)])  # 105 modules on 9 mm
    with pytest.raises(CodeTooLarge):
        get_backend("vc500w").prepare(label, PrintOptions())


def test_the_render_resolution_follows_the_print():
    """Backend.render_dpi: the resolution a label is printed at, from the
    options as the print has them (preview and command line check)."""
    from etiketto.backends.base import PrinterInfo, PrintOptions

    zpl = get_backend("zpl")
    assert zpl.render_dpi(0, PrintOptions(settings={"dpi": "300"})) == pytest.approx(12 * 25.4)
    assert zpl.render_dpi(0, PrintOptions(settings={"dpi": "600"})) == pytest.approx(24 * 25.4)
    ql = get_backend("brother_ql")
    assert ql.render_dpi(62, PrintOptions()) == 300
    assert ql.render_dpi(62, PrintOptions(quality="high")) == 600
    ptouch = get_backend("ptouch")
    assert ptouch.render_dpi(12, PrintOptions()) == 180
    assert ptouch.render_dpi(12, PrintOptions(high_res=True)) == 360
    niimbot = get_backend("niimbot")
    b32 = PrinterInfo("B32", None, None, None, "IDLE")
    assert niimbot.render_dpi(0, PrintOptions(info=b32)) == pytest.approx(300)
    system = get_backend("system")
    office = PrinterInfo("Office", None, None, None, "IDLE", dpi=600)
    assert system.render_dpi(0, PrintOptions(info=office)) == 600
    assert system.render_dpi(0, PrintOptions(info=office), max_dpi=400) == 400
    assert get_backend("ptouch").raster(12, PrintOptions(high_res=True)).across_step == 2
    assert get_backend("vc500w").raster(24, PrintOptions()).across_step == 1


def test_every_continuous_backend_says_what_its_tape_comes_as():
    """Capabilities.media: a cassette or roll the printer recognises, or
    paper whose width the user chooses (one field, no second flag)."""
    for backend in backends():
        assert backend.capabilities.media in ("cassette", "roll", "paper")
    assert get_backend("escpos").capabilities.media == "paper"
    assert get_backend("brother_ql").capabilities.media == "roll"
    assert get_backend("vc500w").capabilities.media == "cassette"


def test_one_map_error_and_one_connect_for_the_tcp_and_usb_printers(monkeypatch):
    """raw, cab, QL and P-touch share base.map_error and net.connect; on
    USB the connection gets the backend's minimum rate like TCP."""
    from etiketto.backends import base, brother_ql, cab, net, ptouch, raw, usb

    assert raw.map_error is cab.map_error is brother_ql.map_error is ptouch.map_error
    assert raw.map_error is base.map_error
    assert type(base.map_error(OSError("unreachable"))) is base.PrinterNotFound
    assert type(base.map_error(ValueError("cannot resolve x"))) is base.PrinterNotFound
    assert type(base.map_error(ValueError("odd"))) is base.PrinterError

    class Connection:
        min_rate = usb.MIN_RATE

    made = []
    monkeypatch.setattr(
        usb, "connect", lambda host, timeout, wait: made.append(host) or Connection()
    )
    link = net.connect("usb://Brother/QL-820NWB", 9100, 10.0, 5.0, brother_ql.MIN_RATE)
    assert made and link.min_rate == brother_ql.MIN_RATE


def test_backends_offer_only_known_extras_and_cut_modes():
    from etiketto.backends import base

    for backend in backends():
        assert set(backend.capabilities.extras) <= set(base._EXTRAS), backend.id
        assert set(backend.capabilities.cut_modes) <= set(base.CUT_MODES), backend.id
