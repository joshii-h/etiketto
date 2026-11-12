"""The OpenAPI 3.1 description of the REST API, written by hand.

Served at /api/v1/openapi.json. Keep it in step with http.py (ROUTES),
requests.py (the fields) and service.py (the answers); a test checks that
every route is described.
"""

from __future__ import annotations

from typing import Any

from tavletta import __version__
from tavletta.merge import limits
from tavletta.model import MAX_COUNTER
from tavletta.server.requests import MAX_COPIES, MAX_WAIT


def _ref(name: str) -> dict:
    return {"$ref": "#/components/schemas/" + name}


def _json(schema: dict, description: str) -> dict:
    return {"description": description, "content": {"application/json": {"schema": schema}}}


def _error(description: str) -> dict:
    return {"$ref": "#/components/responses/Error", "description": description}


_TEXTS = {"type": "object", "additionalProperties": {"type": ["string", "number"]}}

LABEL_FIELDS: dict[str, Any] = {
    "label": {
        "type": "string",
        "description": "Name of a label file in the labels folder (see GET /labels); "
        "'.tavletta' may be left out.",
    },
    "label_content": {
        "type": "object",
        "description": "Or the label itself, as the JSON of a .tavletta file. Its data "
        "file is ignored: send data_rows instead.",
    },
    "printer": {
        "type": "string",
        "description": "With printers.toml on the server: the printer's name (default: "
        "the default printer, or the only one). Else a printer model id, if not the "
        "label's own.",
    },
    "set": dict(
        _TEXTS,
        description="Element name or variable name -> value; variables in a value are "
        "filled in; 'date' sets the moment {=date} and {=time} start from.",
    ),
    "set_literal": dict(_TEXTS, description="Like set; braces in a value are no variables."),
    "counter_start": {"type": "integer", "minimum": 0, "maximum": MAX_COUNTER},
    "data_rows": {
        "type": "array",
        "minItems": 1,
        "maxItems": limits.MAX_ROWS,
        "items": {
            "type": "object",
            "additionalProperties": {"type": ["string", "number", "null"]},
        },
        "description": "Serial labels: one label per row, column name -> value.",
    },
    "data_file": {
        "type": "string",
        "description": "Or a CSV, ODS or XLSX file in the labels folder. Without either, "
        "the label's own data file is used if it lies in the labels folder.",
    },
    "sheet": {"type": "string", "description": "Sheet of the data file, the first by default."},
}

SCHEMAS: dict[str, Any] = {
    "Error": {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "description": "Stable, for programs."},
                    "message": {
                        "type": "string",
                        "description": "Translated after Accept-Language.",
                    },
                },
            }
        },
    },
    "Warning": {
        "type": "object",
        "required": ["code", "message"],
        "properties": {"code": {"type": "string"}, "message": {"type": "string"}},
    },
    "Health": {
        "type": "object",
        "required": ["status", "version"],
        "properties": {"status": {"const": "ok"}, "version": {"type": "string"}},
    },
    "NamedPrinter": {
        "type": "object",
        "description": "A printer of printers.toml.",
        "required": ["name", "printer", "host"],
        "properties": {
            "name": {"type": "string"},
            "printer": {"type": "string", "description": "Printer model id."},
            "model": {"type": "string"},
            "host": {"type": "string"},
            "description": {"type": ["string", "null"]},
            "location": {"type": ["string", "null"]},
            "media": {"type": ["string", "null"]},
            "default": {"type": "boolean"},
            "max_copies": {"type": "integer"},
            "status": {
                "description": "The state (as PrinterStatus), or UNREACHABLE with an error; "
                "null with status=false.",
                "type": ["object", "null"],
            },
        },
    },
    "Printer": {
        "type": "object",
        "required": ["id", "name", "kind", "found"],
        "properties": {
            "id": {"type": "string"},
            "name": {"type": "string"},
            "kind": {"enum": ["tape", "labels", "pages"]},
            "dpi": {"type": "number"},
            "color_model": {"enum": ["color", "mono", "two-color"]},
            "tape_widths_mm": {"type": "array", "items": {"type": "number"}},
            "label_sizes_mm": {
                "type": "array",
                "items": {"type": "array", "items": {"type": "number"}},
            },
            "configured_host": {"type": ["string", "null"]},
            "found": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "host": {"type": "string"},
                        "address": {"type": "string"},
                    },
                },
            },
        },
    },
    "PrinterStatus": {
        "type": "object",
        "required": ["printer", "host", "state"],
        "properties": {
            "printer": {"type": "string"},
            "host": {"type": "string"},
            "name": {"type": "string", "description": "Its name in printers.toml."},
            "model": {"type": "string"},
            "state": {"type": "string"},
            "error": {"oneOf": [{"type": "null"}, _ref("Warning")]},
            "dpi": {"type": "number"},
            "firmware": {"type": ["string", "null"]},
            "modes": {"type": "array", "items": {"type": "string"}},
            "media": {"type": "object"},
        },
    },
    "LabelFile": {
        "type": "object",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "bytes": {"type": "integer"},
            "modified": {"type": "string", "format": "date-time"},
        },
    },
    "Label": {
        "type": "object",
        "required": ["name", "printer", "elements"],
        "properties": {
            "name": {"type": "string"},
            "printer": {"type": "object"},
            "size": {"type": "object"},
            "elements": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "kind": {"enum": ["text", "code", "image"]},
                        "name": {
                            "type": ["string", "null"],
                            "description": "What 'set' fills in.",
                        },
                        "text": {"type": "string"},
                        "data": {"type": "string"},
                        "symbology": {"type": "string"},
                        "file": {"type": ["string", "null"]},
                    },
                },
            },
            "variables": {"type": "array", "items": {"type": "string"}},
            "builtin_variables": {"type": "array", "items": {"type": "string"}},
            "columns": {"type": "array", "items": {"type": "string"}},
            "data_file": {"type": ["string", "null"]},
            "data_file_unusable": {"type": "boolean"},
            "data_sheet": {"type": ["string", "null"]},
            "counter": {"type": "object"},
            "features": {"type": "array", "items": {"type": "string"}},
        },
    },
    "PreviewRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(
            LABEL_FIELDS,
            row={"type": "integer", "minimum": 1, "description": "Data row to show."},
            format={"enum": ["png", "json"], "default": "png"},
            view={
                "enum": ["preview", "printer"],
                "default": "preview",
                "description": "'printer': the image as the printer gets it (turned).",
            },
        ),
    },
    "Preview": {
        "type": "object",
        "required": ["width", "height", "warnings", "png"],
        "properties": {
            "width": {"type": "integer"},
            "height": {"type": "integer"},
            "warnings": {"type": "array", "items": _ref("Warning")},
            "png": {"type": "string", "contentEncoding": "base64"},
        },
    },
    "PrintRequest": {
        "type": "object",
        "additionalProperties": False,
        "properties": dict(
            LABEL_FIELDS,
            host={
                "type": "string",
                "description": "Printer address, only without printers.toml; the one set "
                "in the app by default.",
            },
            copies={"type": "integer", "minimum": 1, "maximum": MAX_COPIES, "default": 1},
            rows={"type": "string", "description": "Data rows: 3-10, 5, 7- or -4."},
            no_save_counter={"type": "boolean", "default": False},
            wait={
                "type": "number",
                "minimum": 0,
                "maximum": MAX_WAIT,
                "default": 0,
                "description": "Seconds to wait for the job to finish before answering.",
            },
        ),
    },
    "Job": {
        "type": "object",
        "required": ["id", "state", "printed", "total"],
        "properties": {
            "id": {"type": "string"},
            "state": {"enum": ["queued", "waiting", "printing", "done", "failed", "canceled"]},
            "printer": {"type": "string"},
            "printer_name": {"type": ["string", "null"]},
            "host": {"type": "string"},
            "owner": {"type": ["string", "null"], "description": "Name of the token."},
            "label": {"type": ["string", "null"]},
            "printed": {"type": "integer", "description": "Labels printed so far."},
            "total": {"type": "integer"},
            "rows_printed": {"type": "integer"},
            "cancel_requested": {"type": "boolean"},
            "error": {"oneOf": [{"type": "null"}, _ref("Warning")]},
            "remark": {"type": ["string", "null"]},
            "warnings": {"type": "array", "items": _ref("Warning")},
            "created": {"type": "string", "format": "date-time"},
            "started": {"type": ["string", "null"], "format": "date-time"},
            "finished": {"type": ["string", "null"], "format": "date-time"},
        },
    },
}

_LANGUAGE = {
    "name": "Accept-Language",
    "in": "header",
    "required": False,
    "schema": {"type": "string"},
    "description": "Language of the messages, e.g. de-CH.",
}
_ASK = {
    "name": "X-Tavletta-Request",
    "in": "header",
    "required": False,
    "schema": {"type": "string"},
    "description": "Needed (any value) when the server has no token: a web page cannot "
    "send it, so it cannot make the server contact printers.",
}
_JOB_ID = {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}


def _operation(summary: str, responses: dict, **extra) -> dict:
    """An operation with the answers every request can get."""
    if extra.get("security") != []:
        responses.setdefault("401", _error("Access token missing, wrong, expired or revoked"))
        responses.setdefault("429", _error("Too many wrong tokens from this address"))
    responses.setdefault(
        "403",
        _error(
            "Not allowed for this token, a printer it may not use, a web page "
            "(Origin) not allowed, or X-Tavletta-Request missing (no token, a request "
            "that contacts printers)"
        ),
    )
    responses.setdefault("405", _error("Not a method of this path"))
    responses.setdefault("408", _error("The request took longer than 30 s to send"))
    responses.setdefault("500", _error("An error of the server (logged)"))
    responses.setdefault("503", _error("Busy: no request slot, render slot or thread free"))
    responses.setdefault("501", {"description": "A method HTTP knows but the server does not"})
    responses.setdefault("421", _error("Not a loopback Host on a loopback server"))
    if "requestBody" in extra:
        responses.setdefault("411", _error("No Content-Length"))
        responses.setdefault("413", _error("Too large (--max-body, 4 MB by default)"))
        responses.setdefault("415", _error("Not application/json"))
    operation: dict[str, Any] = {
        "summary": summary,
        "parameters": [_LANGUAGE],
        "responses": responses,
    }
    operation.update(extra)
    return operation


def _paths() -> dict:
    return {
        "/health": {
            "get": _operation(
                "Is the server running (no token needed)",
                {"200": _json(_ref("Health"), "Running")},
                security=[],
            )
        },
        "/openapi.json": {
            "get": _operation("This description", {"200": _json({"type": "object"}, "OpenAPI")})
        },
        "/printers": {
            "get": _operation(
                "The printers of printers.toml with their state; without it the printer "
                "models, the address set for each and the printers found (--discover)",
                {
                    "200": _json(
                        {
                            "type": "object",
                            "properties": {
                                "printers": {
                                    "type": "array",
                                    "items": {"oneOf": [_ref("NamedPrinter"), _ref("Printer")]},
                                }
                            },
                        },
                        "Printers",
                    )
                },
                parameters=[
                    _LANGUAGE,
                    _ASK,
                    {
                        "name": "discover",
                        "in": "query",
                        "schema": {"type": "boolean", "default": True},
                    },
                    {
                        "name": "status",
                        "in": "query",
                        "schema": {"type": "boolean", "default": True},
                        "description": "Ask each printer of printers.toml for its state.",
                    },
                ],
            )
        },
        "/printers/{id}/status": {
            "get": _operation(
                "Ask a printer for its state: media, problems, resolution",
                {
                    "200": _json(_ref("PrinterStatus"), "State"),
                    "400": _error("No address"),
                    "403": _error("Address not allowed"),
                    "404": _error("Unknown printer model"),
                    "502": _error("The printer cannot be reached or reports a problem"),
                },
                parameters=[
                    _LANGUAGE,
                    _ASK,
                    {
                        "name": "id",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "Printer name (printers.toml) or printer model id.",
                    },
                    {
                        "name": "host",
                        "in": "query",
                        "schema": {"type": "string"},
                        "description": "Only without printers.toml.",
                    },
                ],
            )
        },
        "/labels": {
            "get": _operation(
                "The label files in the labels folder",
                {
                    "200": _json(
                        {
                            "type": "object",
                            "properties": {
                                "labels": {"type": "array", "items": _ref("LabelFile")}
                            },
                        },
                        "Labels",
                    )
                },
            )
        },
        "/labels/{name}": {
            "get": _operation(
                "Elements, variables, data columns and size of a label",
                {
                    "200": _json(_ref("Label"), "Label"),
                    "400": _error("Not a name inside the labels folder"),
                    "404": _error("No such label"),
                    "422": _error("The file is no label or damaged"),
                },
                parameters=[
                    _LANGUAGE,
                    {
                        "name": "name",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                        "description": "May contain slashes for subfolders.",
                    },
                ],
            )
        },
        "/preview": {
            "post": _operation(
                "The label filled in, as an image",
                {
                    "200": {
                        "description": "PNG, or JSON with format=json. Warnings (codes) also "
                        "in the header X-Tavletta-Warnings.",
                        "content": {
                            "image/png": {"schema": {"type": "string", "format": "binary"}},
                            "application/json": {"schema": _ref("Preview")},
                        },
                    },
                    "400": _error("Invalid request"),
                    "404": _error("No such label, printer or data file"),
                    "413": _error("Too large"),
                    "422": _error("The label or its data cannot be used"),
                },
                requestBody={"required": True, **_json(_ref("PreviewRequest"), "What to show")},
            )
        },
        "/print": {
            "post": _operation(
                "Print: the job is queued, one job per printer at a time",
                {
                    "200": _json(_ref("Job"), "The job finished within 'wait'"),
                    "202": _json(_ref("Job"), "Queued or printing; see Location"),
                    "400": _error("Invalid request"),
                    "403": _error("Address not allowed"),
                    "404": _error("No such label, printer or data file"),
                    "409": _error("The counter could not be saved afterwards"),
                    "413": _error("Too large"),
                    "422": _error("The label would print wrongly"),
                    "429": _error("Too many jobs waiting"),
                },
                requestBody={"required": True, **_json(_ref("PrintRequest"), "What to print")},
            )
        },
        "/jobs/{id}": {
            "get": _operation(
                "State of a print job",
                {"200": _json(_ref("Job"), "Job"), "404": _error("No such job")},
                parameters=[_LANGUAGE, _JOB_ID],
            ),
            "delete": _operation(
                "Cancel a job: at once while it waits, after the current label while it prints",
                {
                    "202": _json(_ref("Job"), "Canceled or stopping"),
                    "404": _error("No such job"),
                    "409": _error("Already finished"),
                },
                parameters=[_LANGUAGE, _JOB_ID],
            ),
        },
    }


def spec(mcp: bool = False) -> dict:
    description = (
        "Print labels made with Tavletta. All requests but /health need "
        "'Authorization: Bearer <token>' when the server has a token. Named tokens "
        "have a role: preview (look only), print (also print and follow their own "
        "jobs) or admin (also all jobs)."
    )
    if mcp:
        description += " MCP (Streamable HTTP) is served at /mcp."
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Tavletta",
            "version": __version__,
            "description": description,
            "license": {"name": "AGPL-3.0-or-later", "identifier": "AGPL-3.0-or-later"},
        },
        "servers": [{"url": "/api/v1"}],
        "security": [{"bearer": []}],
        "paths": _paths(),
        "components": {
            "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
            "schemas": SCHEMAS,
            "responses": {
                "Error": {
                    "description": "An error",
                    "content": {"application/json": {"schema": _ref("Error")}},
                }
            },
        },
    }
