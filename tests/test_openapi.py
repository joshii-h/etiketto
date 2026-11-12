"""The OpenAPI description: docs/openapi.yaml is valid, describes exactly the
routes the server has, is up to date with tavletta/server/openapi.py (as is
the API reference page), and the server serves it to every valid token."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

from tavletta.server import config as cfg
from tavletta.server import requests
from tavletta.server.http import ROUTES
from tavletta.server.openapi import CODES, ERROR_CODES, ROLE, spec
from tests.test_server import call

ROOT = Path(__file__).resolve().parent.parent
YAML_FILE = ROOT / "docs" / "openapi.yaml"
METHODS = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}


def _document() -> dict:
    return yaml.safe_load(YAML_FILE.read_text(encoding="utf-8"))


def _tool():
    spec_ = importlib.util.spec_from_file_location("openapi_docs", ROOT / "tools/openapi_docs.py")
    module = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(module)
    return module


def _resolve(document: dict, ref: str) -> object:
    assert ref.startswith("#/"), ref
    node: object = document
    for part in ref[2:].split("/"):
        assert isinstance(node, dict) and part in node, ref
        node = node[part]
    return node


def _refs(node: object):
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            yield node["$ref"]
        for value in node.values():
            yield from _refs(value)
    elif isinstance(node, list):
        for value in node:
            yield from _refs(value)


def test_the_file_is_a_valid_openapi_document():
    document = _document()
    try:  # a full validator if one is installed (not a dependency)
        from openapi_spec_validator import validate
    except ImportError:
        validate = None
    if validate is not None:
        validate(document)
    # the structure, checked here in any case
    assert re.fullmatch(r"3\.1\.\d+", document["openapi"])
    assert document["info"]["title"] and document["info"]["version"]
    schemes = document["components"]["securitySchemes"]
    assert schemes["bearer"] == schemes["bearer"] | {"type": "http", "scheme": "bearer"}
    for ref in _refs(document):
        _resolve(document, ref)
    for path, item in document["paths"].items():
        assert path.startswith("/")
        names = set(re.findall(r"\{(\w+)\}", path))
        for method, operation in item.items():
            assert method in METHODS, (path, method)
            assert operation["summary"] and operation["responses"]
            assert operation[ROLE] in ("none", *cfg.ROLES)
            declared = {p["name"] for p in operation.get("parameters", []) if p["in"] == "path"}
            assert declared == names, (path, method)
            for parameter in operation.get("parameters", []):
                assert parameter["in"] in ("path", "query", "header", "cookie")
                assert "schema" in parameter
                if parameter["in"] == "path":
                    assert parameter["required"] is True
            for status, answer in operation["responses"].items():
                assert re.fullmatch(r"[1-5]\d\d", status), (path, status)
                assert answer["description"]


def _route_paths() -> set[tuple[str, str]]:
    """The routes of http.py as OpenAPI paths: '?' is {id}, '*' {name}."""
    found = set()
    for verb, pattern in ROUTES:
        parts = ["{name}" if p == "*" else ("{id}" if p == "?" else p) for p in pattern]
        found.add((verb.lower(), "/" + "/".join(parts)))
    return found


def test_the_routes_match_exactly():
    described = {
        (method, path)
        for path, item in _document()["paths"].items()
        for method in item
        if method in METHODS
    }
    assert described == _route_paths()


def test_the_file_and_the_page_are_up_to_date():
    tool = _tool()
    for path, text in tool.outputs().items():
        current = path.read_text(encoding="utf-8")
        if path.suffix == ".yaml":  # the version changes with every release
            current = re.sub(r"(?m)^  version: .*$", "", current, count=1)
            text = re.sub(r"(?m)^  version: .*$", "", text, count=1)
        assert current == text, "run python3 tools/openapi_docs.py: %s" % path.name


def test_the_file_is_what_the_server_describes():
    document = _document()
    document["info"]["version"] = spec()["info"]["version"]
    assert document == json.loads(json.dumps(spec()))


def test_every_error_code_of_the_server_is_described():
    """Codes the server's code raises (ApiError, not_found, bad_request) are
    in the catalog, and the catalog has nothing the code never raises."""
    source = "\n".join(p.read_text() for p in (ROOT / "tavletta" / "server").glob("*.py"))
    raised = set(re.findall(r'ApiError\(\s*\d+,\s*"([a-z_]+)"', source))
    raised |= set(re.findall(r'not_found\(\s*"([a-z_]+)"', source))
    call_ = r'bad_request\((?:[^()]|\([^()]*\))*?,\s*"([a-z_]+)"\s*\)'
    raised |= set(re.findall(call_, source))
    raised |= {"bad_request", "invalid_field"}  # errors.py: the defaults
    prefixes = {"printer_", "label_"}  # ApiError(…, "printer_" + code)
    catalog = {code for _status, code, _meaning in ERROR_CODES}
    for code in raised - prefixes:
        assert code in catalog, code
    allowed = raised | {"printer_<code>", "label_not_label", "label_newer", "label_damaged"}
    assert catalog <= allowed, catalog - allowed


def test_every_listed_code_is_in_the_catalog():
    catalog = {code for _status, code, _meaning in ERROR_CODES}
    for item in spec()["paths"].values():
        for operation in item.values():
            for answer in operation["responses"].values():
                for code in answer.get(CODES, []):
                    assert code in catalog or code.startswith("printer_"), code


def test_the_examples_are_valid_requests():
    paths = spec()["paths"]
    preview = paths["/preview"]["post"]["requestBody"]["content"]["application/json"]
    requests.preview_request(preview["example"])
    content = paths["/print"]["post"]["requestBody"]["content"]["application/json"]
    for example in content["examples"].values():
        requests.print_request(example["value"])


# --- served by the server ---------------------------------------------------------


@pytest.fixture
def named_tokens(tmp_path):
    path = tmp_path / "tokens.toml"
    entries = [("viewer", "preview"), ("printer", "print")]
    path.write_text(
        "".join(
            '[tokens.%s]\nrole = "%s"\nhash = "%s"\n' % (name, role, cfg.hash_token(name * 8))
            for name, role in entries
        )
    )
    path.chmod(0o600)
    return cfg.TokenStore(path)


def test_the_server_serves_it_to_every_valid_token(make_server, named_tokens):
    _server, base = make_server(tokens=named_tokens, mcp=False)
    status, _response, body = call(base, "GET", "/api/v1/openapi.json")
    assert status == 401 and body["error"]["code"] == "unauthorized"
    for token in ("viewer" * 8, "printer" * 8):
        status, response, document = call(base, "GET", "/api/v1/openapi.json", token=token)
        assert status == 200
        assert response.getheader("Content-Type").startswith("application/json")
        assert document == json.loads(json.dumps(spec(mcp=False)))
    # the health check stays public
    assert call(base, "GET", "/api/v1/health")[0] == 200
