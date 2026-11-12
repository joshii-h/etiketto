"""printers.toml [printers.<name>.settings]: the printer settings of the app
(print method, print upside down, quality, extras, outlines) for a named
printer, on the REST and the MCP print path; data and the counter from
the request."""

from __future__ import annotations

import pytest
from test_server import call
from test_server_config import post, write
from test_server_mcp import request

from etiketto.backends import get_backend
from etiketto.job import Job
from etiketto.model import Label, TextElement
from etiketto.server.config import ConfigError, load_printers

CAB = """
[printers.lager-cab]
backend = "cab"
host = "cab.local"
default = true

[printers.lager-cab.settings]
method = "transfer"
upside_down = true
"""


@pytest.fixture
def cab_server(make_server, server_env, tmp_path, monkeypatch):
    """A server with a cab printer in printers.toml; its jobs recorded."""
    cab = get_backend("cab")
    jobs: list = []
    monkeypatch.setattr(
        cab,
        "print_label",
        lambda image, host, options, progress=None: jobs.append((image, host, options)),
    )
    printers = load_printers(write(tmp_path / "printers.toml", CAB))
    _server, base = make_server(printers=printers)
    return base, jobs


def _inventory_label() -> dict:
    """A label as an inventory tool sends it: free layout, a field from the
    data row and the counter."""
    label = Label(
        printer="cab",
        auto_length=False,
        orientation="across",
        layout="free",
        elements=[
            TextElement(text="{Inventarnummer}", size_pt=12, x_mm=2, y_mm=2),
            TextElement(text="No. {=counter}", size_pt=8, x_mm=2, y_mm=10),
        ],
    )
    label.set_media_size(50, 25)
    return label.to_dict()


def test_settings_are_read_for_the_model(tmp_path, server_env):
    printers = load_printers(write(tmp_path / "p.toml", CAB))
    settings = printers["lager-cab"].settings
    assert settings["method"] == "transfer" and settings["upside_down"] is True
    ql = load_printers(
        write(
            tmp_path / "ql.toml",
            '[printers.ql]\nbackend = "brother_ql"\nhost = "ql.local"\n'
            '[printers.ql.settings]\nquality = "high"\n',
        )
    )
    assert ql["ql"].settings["quality"] == "high"
    pt = load_printers(
        write(
            tmp_path / "pt.toml",
            '[printers.pt]\nbackend = "ptouch"\nhost = "pt.local"\n'
            "[printers.pt.settings]\nchain = true\nhigh_res = true\nfeed_mm = 5\n",
        )
    )
    assert {k: pt["pt"].settings[k] for k in ("chain", "high_res", "feed_mm")} == {
        "chain": True,
        "high_res": True,
        "feed_mm": 5,
    }


@pytest.mark.parametrize(
    ("settings", "where"),
    [
        ('method = "ribbon"', "settings.method: expected one of: printer, transfer, direct"),
        ('upside_down = "yes"', "settings.upside_down: expected true or false"),
        ("quality = 1", "settings.quality: unknown key"),  # a cab has no qualities
        ("chain = true", "settings.chain: unknown key"),
        ("darkness = 5", "settings.darkness: unknown key"),
        ("outlines = true", "settings.outlines: unknown key"),  # not a page printer
    ],
)
def test_settings_the_model_does_not_have_are_refused(tmp_path, server_env, settings, where):
    text = '[printers.a]\nbackend = "cab"\nhost = "h"\n[printers.a.settings]\n%s\n' % settings
    path = write(tmp_path / "printers.toml", text)
    with pytest.raises(ConfigError) as error:
        load_printers(path)
    assert where in str(error.value)
    level = '[printers.z]\nbackend = "zpl"\nhost = "h"\n[printers.z.settings]\ndarkness = 101\n'
    with pytest.raises(ConfigError, match="settings.darkness: expected a number"):
        load_printers(write(tmp_path / "z.toml", level))


def test_rest_prints_with_the_printer_settings(cab_server, monkeypatch):
    base, jobs = cab_server
    texts: list = []
    real = Job.image
    monkeypatch.setattr(
        Job,
        "image",
        lambda self, label: texts.append([e.text for e in label.elements]) or real(self, label),
    )
    body = {
        "label_content": _inventory_label(),
        "data_rows": [{"Inventarnummer": "100001"}],
        "printer": "lager-cab",
        "copies": 1,
        "counter_start": 4711,
        "wait": 10,
    }
    status, _r, job = call(base, "POST", "/api/v1/print", body=body)
    assert status == 200 and job["state"] == "done", job
    (_image, host, options) = jobs[-1]
    assert host == "cab.local"
    assert dict(options.settings) == {"method": "transfer", "upside_down": True}
    cab = get_backend("cab")
    assert cab.setting(options, "upside_down") is True
    assert cab.setting(options, "method") == "transfer"
    assert texts[-1] == ["100001", "No. 4711"]  # the row and the counter of the request


def test_mcp_prints_with_the_printer_settings(cab_server):
    base, jobs = cab_server
    init = request("initialize", {"protocolVersion": "2025-11-25"})
    _s, response, _a = post(base, init, None)
    session = {"Mcp-Session-Id": response.getheader("Mcp-Session-Id")}
    arguments = {"label_content": _inventory_label(), "data_rows": [{"Inventarnummer": "7"}]}
    _s, _r, answer = post(
        base,
        request("tools/call", {"name": "print_label", "arguments": arguments}, 2),
        None,
        session,
    )
    assert answer["result"]["structuredContent"]["state"] == "done", answer
    assert dict(jobs[-1][2].settings) == {"method": "transfer", "upside_down": True}


def test_a_label_sheet_is_printed_as_in_the_app(server_env, tmp_path):
    """A label on a label sheet: every copy (and every row) a position of
    the sheet, the outlines as printers.toml says."""
    from etiketto.model import SheetChoice
    from etiketto.server.requests import LabelRequest
    from etiketto.server.service import Service
    from etiketto.ui.settings import Settings

    text = (
        '[printers.office]\nbackend = "system"\nhost = "ipp://office.local/ipp/print"\n'
        "[printers.office.settings]\noutlines = true\n"
    )
    printers = load_printers(write(tmp_path / "printers.toml", text))
    service = Service(server_env.labels, (), settings=lambda: Settings({}), printers=printers)
    label = Label(printer="system", orientation="across", elements=[TextElement(text="{N}")])
    label.sheet = SheetChoice("Avery L7160", [2])
    request = LabelRequest(
        content=label.to_dict(), printer="office", data_rows=[{"N": "1"}, {"N": "2"}]
    )
    prepared = service.prepare(request, copies=3, printing=True)
    assert prepared.sheet is not None and prepared.sheet.outlines
    assert prepared.sheet.count == 6 and prepared.sheet.positions == (2,)
    assert prepared.job().sheet is prepared.sheet
