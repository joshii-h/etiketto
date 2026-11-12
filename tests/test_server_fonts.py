"""A label asking for a font the server does not have: a warning, not a
silent fallback (a container without the fonts of the designer's computer)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tavletta import typeset
from tests.test_server import call

MISSING = "Tavletta Font That Is Not There"


def content(font: str) -> dict:
    text = {"kind": "text", "text": "IT-004711", "font": font}
    return {"printer": "fake-mono", "tape_mm": 12, "elements": [text]}


@pytest.fixture
def api(make_server):
    server, base = make_server()
    return SimpleNamespace(server=server, base=base)


def test_font_installed():
    assert typeset.font_installed("Sans")
    assert typeset.font_installed("monospace")
    assert typeset.font_installed("")  # the default, Sans
    assert typeset.font_installed("%s, Sans" % MISSING)  # a list with a fallback
    assert not typeset.font_installed(MISSING)
    assert not typeset.font_installed(MISSING.lower())


def test_preview_warns_about_a_missing_font(api):
    body = {"label_content": content(MISSING), "format": "json"}
    status, _response, answer = call(api.base, "POST", "/api/v1/preview", body=body)
    assert status == 200
    codes = [w["code"] for w in answer["warnings"]]
    assert codes == ["font_missing"]
    assert MISSING in answer["warnings"][0]["message"]
    body = {"label_content": content(MISSING)}
    status, response, _png = call(api.base, "POST", "/api/v1/preview", body=body)
    assert status == 200 and response.getheader("X-Tavletta-Warnings") == "font_missing"


def test_preview_with_an_installed_font_has_no_warning(api):
    body = {"label_content": content("Sans"), "format": "json"}
    status, _response, answer = call(api.base, "POST", "/api/v1/preview", body=body)
    assert status == 200 and answer["warnings"] == []


def test_print_job_carries_the_warning(api):
    body = {"label_content": content(MISSING), "wait": 10}
    status, _response, job = call(api.base, "POST", "/api/v1/print", body=body)
    assert status == 200 and job["state"] == "done"
    assert [w["code"] for w in job["warnings"]] == ["font_missing"]
