#!/usr/bin/env python3
"""Write the API reference from the server's OpenAPI description.

    python3 tools/openapi_docs.py           write docs/openapi.yaml and site/docs/api.md
    python3 tools/openapi_docs.py --check   only say whether they are up to date

The source is tavletta/server/openapi.py, which the server also serves at
/api/v1/openapi.json. Run this after changing it (tests/test_openapi.py
fails while the two files are out of date), then
`python3 site/build.py --update-po` for the translations of the page.
Needs PyYAML (a dev dependency).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tavletta.server.openapi import (  # noqa: E402
    CODES,
    ERROR_CODES,
    JOB_ERROR_CODES,
    PRINTER_CODES,
    ROLE,
    spec,
)

YAML_FILE = ROOT / "docs" / "openapi.yaml"
PAGE = ROOT / "site" / "docs" / "api.md"
BASE = "$TAVLETTA"  # in the curl examples: the server's address with /api/v1

HEADER = """\
# The OpenAPI 3.1 description of the Tavletta print server's REST API.
# Written by tools/openapi_docs.py from tavletta/server/openapi.py: do not
# edit; the running server serves the same at /api/v1/openapi.json.
"""

INTRO = """\
# API reference

The REST API of the print server (`tavletta serve`), for programs that
print labels: every request with its role, fields, answers and error
codes. A walkthrough for an inventory tool, with code in Python and C#, is
[Connecting an inventory tool](api-guide.md); installing the server is
[Server installation](server-install.md).

This page is made from the OpenAPI 3.1 description of the server
(`tools/openapi_docs.py`). Programs and code generators read the same
description: `docs/openapi.yaml` in the source, or
`GET /api/v1/openapi.json` from a running server.

## Basics { #basics }

- Address: `http://<server>:8790/api/v1`, or the HTTPS address of the
  reverse proxy. The examples use `$TAVLETTA` for it and `$TOKEN` for the
  access token:

  ```sh
  TAVLETTA=https://labels.example.com/api/v1
  TOKEN=tav_0123456789ab_...
  ```

- JSON in and out (UTF-8). Requests with a body need
  `Content-Type: application/json` and a `Content-Length`.
- Every request but `GET /api/v1/health` needs the header
  `Authorization: Bearer <token>`.
- Messages come in the language of `Accept-Language` (e.g. `de-CH`),
  English by default. Codes never change with the language: programs
  check the code, people read the message.
- Unknown fields are refused (`unknown_field`): a typo never prints
  something else than meant.

## Roles { #roles }

Each access token has a role (`tokens.toml`, made with
`tavletta token new`). A role may do what the roles above it may.

| Role | May |
|---|---|
| `preview` | list printers and labels, describe labels, previews, printer state, this description |
| `print` | as `preview`, and print; see and cancel its own jobs |
| `admin` | as `print`, and see and cancel every job |

A token can be limited to some printers of `printers.toml`: the others
are then "not found" (404 `printer_not_found`) for it.

## Errors { #errors }

Every error is JSON with a stable code and a message for people:

```json
{"error": {"code": "label_not_found", "message": "No such label: asset-tag.tavletta"}}
```

| Status | Code | Meaning |
|---|---|---|
"""

LIMITS = """\
## Limits { #limits }

| What | Limit |
|---|---|
| Request body | 4 MB (`--max-body`) |
| Data rows, columns, cells | 100 000, 1000, 2 000 000 |
| One value or cell | 100 000 characters |
| Copies | 9999, or the printer's `max_copies` |
| Labels in one job (copies times rows) | 10 000 (`--max-labels`) |
| Elements of a label | 200, of them 50 images and codes; 5000 characters and 100 lines a text |
| Jobs waiting or printing | 100 in all, 20 per named token |
| Finished jobs kept | 24 hours, at most 1000 |
| `wait` | 300 seconds |
| Requests at once | 32, 8 per client address |
| Wrong tokens | 10 a minute per address, then 429 for a minute |

## Requests { #requests }

Besides the answers listed with each request, every request can get the
general errors of the table above: 401, 403, 405, 408, 421, 429
(`too_many_requests`), 500 and 503, and one with a body 411, 413
(`too_large`) and 415.

| Request | Role | What |
|---|---|---|
"""


_CODE_LIKE = re.compile(r"(\{[^}\s]*\}|[a-z_]*<code>|--[a-z-]+|\.tavletta|printers\.toml)")


def _md(text: str) -> str:
    """A description for a table cell: placeholders, options and file names
    as code (a {=counter} or <code> would be read as markup), | escaped."""
    return _CODE_LIKE.sub(lambda m: "`%s`" % m.group(1), text).replace("|", "\\|")


def _anchor(method: str, path: str) -> str:
    words = [method.lower()] + [p.strip("{}") for p in path.split("/") if p]
    return "-".join(w.replace(".", "-").replace("_", "-") for w in words)


def _schema(document: dict, schema: dict) -> dict:
    ref = schema.get("$ref", "")
    return document["components"]["schemas"][ref.rsplit("/", 1)[1]] if ref else schema


def _type(schema: dict) -> str:
    if "$ref" in schema:
        return schema["$ref"].rsplit("/", 1)[1]
    if "enum" in schema:
        return " \\| ".join("`%s`" % json.dumps(v) for v in schema["enum"])
    kind = schema.get("type", "")
    if isinstance(kind, list):
        kind = " or ".join(kind)
    if kind == "array":
        return "array of " + (_type(schema.get("items", {})) or "values")
    if "oneOf" in schema:
        return " or ".join(_type(s) for s in schema["oneOf"])
    return kind


def _range(schema: dict) -> str:
    low, high = schema.get("minimum"), schema.get("maximum")
    parts = []
    if low is not None and high is not None:
        parts.append("%s to %s" % (low, high))
    if "default" in schema:
        parts.append("default `%s`" % json.dumps(schema["default"]))
    return "; ".join(parts)


def _fields(document: dict, schema: dict) -> list[str]:
    lines = ["| Field | Type | Meaning |", "|---|---|---|"]
    for name, field in _schema(document, schema)["properties"].items():
        text = " ".join(x for x in (field.get("description", ""), _range(field)) if x)
        lines.append("| `%s` | %s | %s |" % (name, _type(field), _md(text)))
    return lines


def _parameters(operation: dict) -> list[str]:
    params = [p for p in operation.get("parameters", []) if p["name"] != "Accept-Language"]
    if not params:
        return []
    lines = ["| Parameter | In | Meaning |", "|---|---|---|"]
    for p in params:
        text = " ".join(x for x in (p.get("description", ""), _range(p["schema"])) if x)
        lines.append("| `%s` | %s | %s |" % (p["name"], p["in"], _md(text)))
    return lines


def _answers(operation: dict) -> list[str]:
    """The answers of the operation itself; the ones every request can get
    are in the table of errors."""
    common = {"401", "403", "405", "408", "411", "415", "421", "500", "501", "503"}
    lines = ["| Status | Answer | Codes |", "|---|---|---|"]
    for status, answer in operation["responses"].items():
        codes = answer.get(CODES, [])
        if status in common or (status in ("413", "429") and len(codes) < 2):
            continue
        if status == "502":
            codes = ["printer_<code>"]
        shown = ", ".join("`%s`" % c for c in codes)
        lines.append("| %s | %s | %s |" % (status, _md(answer["description"]), shown))
    return lines


def _curl(method: str, path: str, operation: dict) -> list[str]:
    url = BASE + path.replace("{id}", "$JOB").replace("{name}", "asset-tag")
    if path == "/printers/{id}/status":
        url = BASE + "/printers/it-cab/status"
    auth = [] if operation.get("security") == [] else ['-H "Authorization: Bearer $TOKEN"']
    parts = ["curl"] + auth
    if method not in ("get", "post"):
        parts.append("-X " + method.upper())
    body = operation.get("requestBody")
    if body:
        content = body["content"]["application/json"]
        example = content.get("example") or next(iter(content["examples"].values()))["value"]
        parts.append("-H 'Content-Type: application/json'")
        parts.append("-d '%s'" % json.dumps(example, ensure_ascii=False))
        if path == "/preview":
            parts.append("-o preview.png")
    parts.append('"%s"' % url)
    lines = ["```sh", parts[0] + " " + " ".join(parts[1:2]) + " \\"]
    lines += ["     %s \\" % p for p in parts[2:-1]]
    lines.append("     " + parts[-1])
    if len(parts) == 2:
        lines = ["```sh", " ".join(parts)]
    return lines + ["```"]


def _operation(document: dict, method: str, path: str, operation: dict) -> list[str]:
    role = operation[ROLE]
    lines = [
        "### `%s /api/v1%s` { #%s }" % (method.upper(), path, _anchor(method, path)),
        "",
        _md(operation["summary"]) + ".",
        "",
        "- Role: %s" % ("none (no token)" if role == "none" else "`%s`" % role),
        "",
    ]
    if params := _parameters(operation):
        lines += params + [""]
    body = operation.get("requestBody")
    if body:
        lines += ["Body fields:", ""]
        lines += _fields(document, body["content"]["application/json"]["schema"]) + [""]
    lines += _answers(operation) + [""]
    lines += _curl(method, path, operation) + [""]
    return lines


def _schemas(document: dict) -> list[str]:
    lines = ["## Answers { #answers }", ""]
    for name in ("Job", "Label", "NamedPrinter", "PrinterStatus", "Preview"):
        schema = document["components"]["schemas"][name]
        lines += ["### %s { #%s }" % (name, name.lower()), ""]
        if "description" in schema:
            lines += [schema["description"], ""]
        lines += _fields(document, schema)[0:2]
        lines[-2] = "| Field | Type | Meaning |"
        for field, info in schema["properties"].items():
            text = _md(info.get("description", ""))
            lines.append("| `%s` | %s | %s |" % (field, _type(info), text))
        lines.append("")
    job = json.dumps(document["components"]["schemas"]["Job"]["example"], indent=2)
    lines += ["Example of a job:", "", "```json", job, "```", ""]
    return lines


def page(document: dict) -> str:
    lines = [INTRO.rstrip("\n")]
    for status, code, meaning in ERROR_CODES:
        lines.append("| %d | `%s` | %s |" % (status, code, _md(meaning)))
    lines += [
        "",
        "`printer_<code>`: the printer's problem, one of "
        + ", ".join("`%s`" % c for c in PRINTER_CODES)
        + ".",
        "",
        "A job that fails tells why in its `error`, with these codes:",
        "",
        "| Code | Meaning |",
        "|---|---|",
    ]
    lines += ["| `%s` | %s |" % (code, _md(meaning)) for code, meaning in JOB_ERROR_CODES]
    lines += ["", LIMITS.rstrip("\n")]
    operations = [
        (method, path, operation)
        for path, item in document["paths"].items()
        for method, operation in item.items()
    ]
    for method, path, operation in operations:
        role = operation[ROLE]
        lines.append(
            "| [`%s /api/v1%s`](#%s) | %s | %s |"
            % (
                method.upper(),
                path,
                _anchor(method, path),
                "none" if role == "none" else "`%s`" % role,
                _md(operation["summary"]),
            )
        )
    lines.append("")
    for method, path, operation in operations:
        lines += _operation(document, method, path, operation)
    lines += _schemas(document)
    return "\n".join(lines).rstrip("\n") + "\n"


def yaml_text(document: dict) -> str:
    body = yaml.safe_dump(document, sort_keys=False, allow_unicode=True, width=99)
    return HEADER + body


def outputs() -> dict[Path, str]:
    document = spec()
    return {YAML_FILE: yaml_text(document), PAGE: page(document)}


def main() -> int:
    check = "--check" in sys.argv[1:]
    stale = []
    for path, text in outputs().items():
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        if current != text:
            stale.append(path)
            if not check:
                path.write_text(text, encoding="utf-8")
    for path in stale:
        print(("out of date: %s" if check else "wrote %s") % path.relative_to(ROOT))
    return 1 if check and stale else 0


if __name__ == "__main__":
    sys.exit(main())
