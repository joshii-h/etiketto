"""Tests every printer backend has to pass.

For a new backend, subclass BackendContract in its test module and provide a
`harness` fixture (see test_vc500w.TestContract and test_backends.TestFakeMono):

    harness.backend   the backend instance, talking to a fake printer
    harness.fail()    make the fake printer report an error
    harness.printed() number of labels the fake printer received

The fake must never contact a real printer or the network.
test_backends.test_every_backend_has_a_contract_test checks that each
registered backend has such a subclass.
"""

from __future__ import annotations

import pytest
from PIL import Image

from etiketto.backends.base import (
    CUT_MODES,
    Backend,
    FoundPrinter,
    PrinterError,
    PrinterInfo,
    PrintOptions,
)
from etiketto.model import ImageElement, Label
from etiketto.render import mm_to_px


def _label(tape_mm, png_b64) -> Label:
    return Label(
        tape_mm=tape_mm,
        background="#ff8800",
        elements=[ImageElement(data=png_b64((30, 20), "blue"))],
    )


class BackendContract:
    backend_id = ""  # registry id of the backend under test

    def test_identity(self, harness):
        backend = harness.backend
        assert isinstance(backend, Backend)
        assert backend.id == self.backend_id
        assert backend.name and isinstance(backend.name, str)

    def test_capabilities(self, harness):
        caps = harness.backend.capabilities
        assert caps.tape_widths_mm and list(caps.tape_widths_mm) == sorted(caps.tape_widths_mm)
        assert all(isinstance(w, int) and w > 0 for w in caps.tape_widths_mm)
        assert caps.dpi > 0
        assert caps.modes and len({m.id for m in caps.modes}) == len(caps.modes)
        assert all(m.lpi > 0 for m in caps.modes)
        assert caps.color_model in ("color", "mono", "two-color")
        assert set(caps.cut_modes) <= set(CUT_MODES)
        assert "jpeg" in caps.data_formats or caps.data_formats

    def test_prepare_matches_the_raster(self, harness, png_b64):
        backend = harness.backend
        caps = backend.capabilities
        for tape in caps.tape_widths_mm:
            for mode in caps.modes:
                for data_format in caps.data_formats:
                    options = PrintOptions(mode=mode.id, data_format=data_format)
                    raster = backend.raster(tape, options)
                    assert raster.dpi > 0 and raster.bleed_px >= 0
                    image = backend.prepare(_label(tape, png_b64), options)
                    tape_px = raster.tape_px or mm_to_px(tape, raster.dpi)
                    expected = raster.width_px or tape_px + 2 * raster.bleed_px
                    assert image.height == expected, (tape, mode, data_format)
                    assert image.mode == "RGB"

    def test_mono_printers_get_black_and_white(self, harness, png_b64):
        backend = harness.backend
        image = backend.prepare(
            _label(backend.capabilities.tape_widths_mm[0], png_b64), PrintOptions()
        )
        colours = {c for _n, c in image.getcolors(maxcolors=1 << 24)}
        if backend.capabilities.color_model == "mono":
            assert colours <= {(0, 0, 0), (255, 255, 255)}
        else:
            assert (255, 136, 0) in colours  # the background colour survives

    def test_query(self, harness):
        info = harness.backend.query("printer.invalid")
        assert isinstance(info, PrinterInfo)
        assert info.tape_mm is None or info.tape_mm in harness.backend.capabilities.tape_widths_mm

    def test_print_copies_with_progress(self, harness, png_b64):
        backend = harness.backend
        caps = backend.capabilities
        options = PrintOptions(
            copies=2, cut=(caps.cut_modes or ("",))[0], mode=backend.modes(None)[0].id
        )
        image = backend.prepare(_label(caps.tape_widths_mm[0], png_b64), options)
        events = []
        backend.print_label(image, "printer.invalid", options, events.append)
        assert harness.printed() == 2
        assert {e.copy for e in events} == {1, 2} and {e.copies for e in events} == {2}

    def test_print_errors_are_printer_errors(self, harness):
        harness.fail()
        with pytest.raises(PrinterError):
            harness.backend.print_label(
                Image.new("RGB", (200, 100), "white"), "printer.invalid", PrintOptions()
            )
        assert harness.printed() == 0

    def test_discover(self, harness):
        found = harness.backend.discover(timeout=0.01)
        assert isinstance(found, list) and all(isinstance(f, FoundPrinter) for f in found)
