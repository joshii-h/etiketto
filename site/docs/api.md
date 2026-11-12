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
| 400 | `bad_request` | The request cannot be read (e.g. a body that ends early). |
| 400 | `not_json` | The body is no valid JSON (415 with another Content-Type). |
| 400 | `invalid_field` | A field has a wrong type or value; the message names it. |
| 400 | `unknown_field` | A field the request does not have (a typo): nothing is done. |
| 400 | `no_label` | Give either label or label_content. |
| 400 | `data_conflict` | Give data_rows or data_file, not both. |
| 400 | `no_rows` | rows selects no data row. |
| 400 | `no_data` | sheet without a data file. |
| 400 | `no_printer` | Several printers and no default: name one in printer. |
| 400 | `no_host` | No printer address (without `printers.toml`). |
| 400 | `host_given` | host with `printers.toml`: choose the printer by name. |
| 400 | `too_many_copies` | More copies than the printer's max_copies. |
| 401 | `unauthorized` | Token missing, wrong, expired or revoked. |
| 403 | `forbidden` | The token's role is too low (e.g. preview printing). |
| 403 | `host_not_allowed` | The printer address is not allowed (`--allow-printer`). |
| 403 | `origin_not_allowed` | A web page of an origin not allowed (`--allow-origin`). |
| 403 | `header_required` | No token on the server: send X-Tavletta-Request. |
| 404 | `not_found` | No such path. |
| 404 | `label_not_found` | No such label file in the labels folder. |
| 404 | `data_not_found` | No such data file in the labels folder. |
| 404 | `printer_not_found` | No such printer, or one the token may not use. |
| 404 | `job_not_found` | No such job (another token's, or older than 24 hours). |
| 405 | `method_not_allowed` | Not a method of this path. |
| 408 | `timeout` | The request took longer than 30 s to send. |
| 409 | `job_finished` | The job has finished: nothing to cancel. |
| 409 | `counter_not_saved` | The label file is not writable: nothing was printed. |
| 411 | `length_required` | No Content-Length (chunked bodies are not accepted). |
| 413 | `too_large` | Body over `--max-body`, or too many data rows, columns or cells. |
| 413 | `too_many_labels` | Copies times rows over `--max-labels` (10000 by default). |
| 413 | `label_too_large` | The label is too large to render (elements, text, pixels). |
| 415 | `not_json` | Send Content-Type: application/json. |
| 421 | `wrong_host` | A loopback server asked for another host name. |
| 422 | `file_problem` | The label file cannot be opened. |
| 422 | `label_not_label` | The file is no Tavletta label. |
| 422 | `label_newer` | The label needs a newer Tavletta: update the server. |
| 422 | `label_damaged` | The label file is damaged. |
| 422 | `label_unprintable` | It would print wrongly: a missing column, a bad code, … |
| 422 | `pages_not_allowed` | Document pages in label_content need the server switch `--allow-pdf-pages`. |
| 422 | `data_outside` | The label's data file lies outside the labels folder. |
| 422 | `data_problem` | The data file cannot be read, or has no such sheet. |
| 422 | `prepare_failed` | The label cannot be prepared for the printer. |
| 429 | `too_many_requests` | Ten wrong tokens from this address: wait a minute. |
| 429 | `queue_full` | Too many jobs waiting: retry later. |
| 500 | `internal` | An error of the server; it is logged. |
| 502 | `printer_<code>` | The printer is unreachable or reports a problem. |
| 503 | `busy` | All request or render slots taken: retry after a moment. |

`printer_<code>`: the printer's problem, one of `generic`, `not_found`, `timeout`, `stalled`, `port_closed`, `busy`, `paused`, `cover_open`, `head_open`, `jam`, `media_jam`, `out_of_labels`, `out_of_paper`, `label_not_found`, `wrong_media`, `paper_size`, `page_too_large`, `too_long`, `too_wide`, `code_too_large`, `ribbon`, `ribbon_out`, `ribbon_direct`, `no_cassette`, `bad_cassette`, `supplies`, `format`, `resolution`, `queue_paused`, `job_held`, `job_canceled`, `usb_not_found`, `usb_permission`, `serial_permission`, `battery`, `overheat`, `unsupported_model`, `unknown_model`, `editor_lite`.

A job that fails tells why in its `error`, with these codes:

| Code | Meaning |
|---|---|
| `printer_<code>` | The printer is unreachable or reports a problem (jam, out of labels…). |
| `counter_not_saved` | Printed, but the next counter value could not be saved. |
| `label_locked` | Another print held the label file too long. |
| `prepare_failed` | The label could not be prepared for the printer. |

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
| [`GET /api/v1/health`](#get-health) | none | Is the server running; no token needed (for health checks) |
| [`GET /api/v1/openapi.json`](#get-openapi-json) | `preview` | This description (OpenAPI 3.1); a token of any role |
| [`GET /api/v1/printers`](#get-printers) | `preview` | The printers of `printers.toml` the token may use, with their state; without it the printer models, the address set for each and the printers found (`--discover`) |
| [`GET /api/v1/printers/{id}/status`](#get-printers-id-status) | `preview` | Ask a printer for its state: media, problems, resolution |
| [`GET /api/v1/labels`](#get-labels) | `preview` | The label files in the labels folder and its subfolders |
| [`GET /api/v1/labels/{name}`](#get-labels-name) | `preview` | What a label can be filled in with: elements, variables, data columns, size, counter |
| [`POST /api/v1/preview`](#post-preview) | `preview` | The label filled in, as an image; nothing is printed |
| [`POST /api/v1/print`](#post-print) | `print` | Print: the request is checked, then queued; each printer prints one job at a time |
| [`GET /api/v1/jobs/{id}`](#get-jobs-id) | `print` | State of a print job (a print token: its own jobs; admin: all). Kept 24 hours after it finished |
| [`DELETE /api/v1/jobs/{id}`](#delete-jobs-id) | `print` | Cancel a job: at once while it waits, after the current label while it prints |

### `GET /api/v1/health` { #get-health }

Is the server running; no token needed (for health checks).

- Role: none (no token)

| Status | Answer | Codes |
|---|---|---|
| 200 | Running |  |

```sh
curl "$TAVLETTA/health"
```

### `GET /api/v1/openapi.json` { #get-openapi-json }

This description (OpenAPI 3.1); a token of any role.

- Role: `preview`

| Status | Answer | Codes |
|---|---|---|
| 200 | OpenAPI |  |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     "$TAVLETTA/openapi.json"
```

### `GET /api/v1/printers` { #get-printers }

The printers of `printers.toml` the token may use, with their state; without it the printer models, the address set for each and the printers found (`--discover`).

- Role: `preview`

| Parameter | In | Meaning |
|---|---|---|
| `X-Tavletta-Request` | header | Needed (any value) when the server has no token: a web page cannot send it, so it cannot make the server contact printers. |
| `discover` | query | Without `printers.toml` and with `--discover`: search. default `true` |
| `status` | query | Ask each printer of `printers.toml` for its state (remembered for 5 s). default `true` |

| Status | Answer | Codes |
|---|---|---|
| 200 | Printers |  |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     "$TAVLETTA/printers"
```

### `GET /api/v1/printers/{id}/status` { #get-printers-id-status }

Ask a printer for its state: media, problems, resolution.

- Role: `preview`

| Parameter | In | Meaning |
|---|---|---|
| `X-Tavletta-Request` | header | Needed (any value) when the server has no token: a web page cannot send it, so it cannot make the server contact printers. |
| `id` | path | Printer name (`printers.toml`) or printer model id. |
| `host` | query | Only without `printers.toml`; needs the role print. |

| Status | Answer | Codes |
|---|---|---|
| 200 | State |  |
| 400 | No address, or host given with `printers.toml` | `no_host`, `host_given`, `invalid_field` |
| 404 | Unknown printer, or one the token may not use | `printer_not_found` |
| 502 | The printer cannot be reached or reports a problem | `printer_<code>` |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     "$TAVLETTA/printers/it-cab/status"
```

### `GET /api/v1/labels` { #get-labels }

The label files in the labels folder and its subfolders.

- Role: `preview`

| Status | Answer | Codes |
|---|---|---|
| 200 | Labels |  |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     "$TAVLETTA/labels"
```

### `GET /api/v1/labels/{name}` { #get-labels-name }

What a label can be filled in with: elements, variables, data columns, size, counter.

- Role: `preview`

| Parameter | In | Meaning |
|---|---|---|
| `name` | path | May contain slashes for subfolders; '`.tavletta`' may be left out. |

| Status | Answer | Codes |
|---|---|---|
| 200 | Label |  |
| 400 | Not a name inside the labels folder | `invalid_field` |
| 404 | No such label | `label_not_found` |
| 422 | The file is no label or damaged | `file_problem`, `label_not_label`, `label_newer`, `label_damaged` |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     "$TAVLETTA/labels/asset-tag"
```

### `POST /api/v1/preview` { #post-preview }

The label filled in, as an image; nothing is printed.

- Role: `preview`

Body fields:

| Field | Type | Meaning |
|---|---|---|
| `label` | string | Name of a label file in the labels folder (see GET /labels), subfolders with '/'; '`.tavletta`' may be left out. Either label or label_content. |
| `label_content` | object | Or the label itself, as the JSON of a `.tavletta` file, checked like a file that is opened. Its data file is ignored (send data_rows instead), its counter never saved. |
| `printer` | string | With `printers.toml` on the server: the printer's name (default: the default printer the token may use, or the only one). Without it: a printer model id, if not the label's own (the label is fitted as in the app). |
| `set` | object | Element name or variable name -> value, as `--set` on the command line; variables in a value are filled in; 'date' sets the moment `{=date}` and `{=time}` start from (ISO 8601). At most 1000 entries. |
| `set_literal` | object | Like set, but braces in a value are text, not variables. |
| `counter_start` | integer | The `{=counter}` of the first label; it goes up by the label's step with every label, copy and row. 0 to 999999999999999 |
| `data_rows` | array of object | Serial labels: one label per row, column name -> value; the label's `{Column}` placeholders take the values. Numbers are taken as text, null as an empty cell. At most 100000 rows, 1000 columns, 2000000 cells, 100000 characters a value. |
| `data_file` | string | Or a CSV, TSV, ODS or XLSX file in the labels folder. Without data_rows and data_file, the label's own data file is used if it lies beside the label in the labels folder. |
| `sheet` | string | Sheet of the data file, the first by default. |
| `row` | integer | Data row to show, from 1. 1 to 100000; default `1` |
| `format` | `"png"` \| `"json"` | 'json': width, height, warnings and the PNG in base64. default `"png"` |
| `view` | `"preview"` \| `"printer"` | 'printer': the image as the printer gets it (turned, in its colours). default `"preview"` |

| Status | Answer | Codes |
|---|---|---|
| 200 | PNG, or JSON with format=json. Warning codes also in the header X-Tavletta-Warnings (comma-separated). |  |
| 400 | Invalid request | `bad_request`, `invalid_field`, `unknown_field`, `no_label`, `data_conflict`, `not_json`, `no_rows`, `no_data`, `no_printer`, `too_many_copies` |
| 404 | No such label, printer or data file | `label_not_found`, `printer_not_found`, `data_not_found` |
| 413 | Too large: body, data rows or the label to render | `too_large`, `label_too_large` |
| 422 | The label or its data cannot be used | `file_problem`, `label_not_label`, `label_newer`, `label_damaged`, `label_unprintable`, `data_outside`, `data_problem`, `prepare_failed` |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag", "printer": "it-cab", "data_rows": [{"AssetNumber": "100001"}], "format": "png"}' \
     -o preview.png \
     "$TAVLETTA/preview"
```

### `POST /api/v1/print` { #post-print }

Print: the request is checked, then queued; each printer prints one job at a time.

- Role: `print`

Body fields:

| Field | Type | Meaning |
|---|---|---|
| `label` | string | Name of a label file in the labels folder (see GET /labels), subfolders with '/'; '`.tavletta`' may be left out. Either label or label_content. |
| `label_content` | object | Or the label itself, as the JSON of a `.tavletta` file, checked like a file that is opened. Its data file is ignored (send data_rows instead), its counter never saved. |
| `printer` | string | With `printers.toml` on the server: the printer's name (default: the default printer the token may use, or the only one). Without it: a printer model id, if not the label's own (the label is fitted as in the app). |
| `set` | object | Element name or variable name -> value, as `--set` on the command line; variables in a value are filled in; 'date' sets the moment `{=date}` and `{=time}` start from (ISO 8601). At most 1000 entries. |
| `set_literal` | object | Like set, but braces in a value are text, not variables. |
| `counter_start` | integer | The `{=counter}` of the first label; it goes up by the label's step with every label, copy and row. 0 to 999999999999999 |
| `data_rows` | array of object | Serial labels: one label per row, column name -> value; the label's `{Column}` placeholders take the values. Numbers are taken as text, null as an empty cell. At most 100000 rows, 1000 columns, 2000000 cells, 100000 characters a value. |
| `data_file` | string | Or a CSV, TSV, ODS or XLSX file in the labels folder. Without data_rows and data_file, the label's own data file is used if it lies beside the label in the labels folder. |
| `sheet` | string | Sheet of the data file, the first by default. |
| `host` | string | Printer address, only without `printers.toml` (default: the one set in the app, else with `--discover` the only printer found). Refused (host_given) with `printers.toml`. |
| `copies` | integer | Copies of each label (each row); at most the printer's max_copies. Copies times rows: at most `--max-labels` (10000 by default). 1 to 9999; default `1` |
| `rows` | string | The data rows to print, from 1: '3-10', '5', '7-' (to the last) or '-4'. To print again from where a job stopped. |
| `no_save_counter` | boolean | Do not save the next `{=counter}` value into the label file. default `false` |
| `wait` | number | Seconds to wait for the job to finish before answering: 200 if it did, else 202. 0 to 300.0; default `0` |

| Status | Answer | Codes |
|---|---|---|
| 200 | The job finished within 'wait' |  |
| 202 | Queued or printing; follow it at Location |  |
| 400 | Invalid request | `bad_request`, `invalid_field`, `unknown_field`, `no_label`, `data_conflict`, `not_json`, `no_rows`, `no_data`, `no_printer`, `no_host`, `host_given`, `too_many_copies` |
| 404 | No such label, printer or data file, or a printer the token may not use | `label_not_found`, `printer_not_found`, `data_not_found` |
| 409 | The label file is not writable: the next counter value could not be saved, nothing was printed | `counter_not_saved` |
| 413 | Too large: body, data rows, labels in one job or the label to render | `too_large`, `too_many_labels`, `label_too_large` |
| 422 | The label would print wrongly | `file_problem`, `label_not_label`, `label_newer`, `label_damaged`, `label_unprintable`, `data_outside`, `data_problem` |
| 429 | Too many wrong tokens, or too many jobs waiting (100 in all, 20 per named token) | `too_many_requests`, `queue_full` |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag", "printer": "it-cab", "data_rows": [{"AssetNumber": "100001"}], "wait": 30}' \
     "$TAVLETTA/print"
```

### `GET /api/v1/jobs/{id}` { #get-jobs-id }

State of a print job (a print token: its own jobs; admin: all). Kept 24 hours after it finished.

- Role: `print`

| Parameter | In | Meaning |
|---|---|---|
| `id` | path | The id of POST /print. |

| Status | Answer | Codes |
|---|---|---|
| 200 | Job |  |
| 404 | No such job | `job_not_found` |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     "$TAVLETTA/jobs/$JOB"
```

### `DELETE /api/v1/jobs/{id}` { #delete-jobs-id }

Cancel a job: at once while it waits, after the current label while it prints.

- Role: `print`

| Parameter | In | Meaning |
|---|---|---|
| `id` | path | The id of POST /print. |

| Status | Answer | Codes |
|---|---|---|
| 202 | Canceled, or stopping after the current label |  |
| 404 | No such job | `job_not_found` |
| 409 | Already finished | `job_finished` |

```sh
curl -H "Authorization: Bearer $TOKEN" \
     -X DELETE \
     "$TAVLETTA/jobs/$JOB"
```

## Answers { #answers }

### Job { #job }

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Random, 16 characters. |
| `state` | `"queued"` \| `"waiting"` \| `"printing"` \| `"done"` \| `"failed"` \| `"canceled"` | waiting: its turn came, another job holds the label file (its counter). done, failed and canceled are final. |
| `printer` | string | Printer model id. |
| `printer_name` | string or null | Its name in `printers.toml`. |
| `host` | string | The printer's address. |
| `owner` | string or null | Name of the token. |
| `label` | string or null | Name in the labels folder; null for label_content. |
| `printed` | integer | Labels printed so far. |
| `total` | integer | Labels to print: copies x rows. |
| `rows_printed` | integer | Data rows printed in full: after a failure, print again with rows set to the next one (rows_printed + first row). |
| `cancel_requested` | boolean | DELETE was sent. |
| `error` | null or Warning | Why it failed: `printer_<code>`, counter_not_saved, label_locked or prepare_failed. |
| `remark` | string or null | A note of the printer, for people. |
| `warnings` | array of Warning | Found when it was checked; it printed anyway. |
| `created` | string | UTC. |
| `started` | string or null |  |
| `finished` | string or null |  |

### Label { #label }

| Field | Type | Meaning |
|---|---|---|
| `name` | string |  |
| `printer` | object | Model id and name. |
| `size` | object | Tape, length, orientation, mm. |
| `elements` | array of object |  |
| `variables` | array of string | Variables of your own, `{=lot}`: filled in with 'set'. |
| `builtin_variables` | array of string |  |
| `columns` | array of string | Placeholders `{Column}`: the keys of data_rows. |
| `data_file` | string or null |  |
| `data_file_unusable` | boolean |  |
| `data_sheet` | string or null |  |
| `counter` | object | used (the label has `{=counter}`), next, step. |
| `features` | array of string |  |

### NamedPrinter { #namedprinter }

A printer of printers.toml.

| Field | Type | Meaning |
|---|---|---|
| `name` | string | What requests name in 'printer'. |
| `printer` | string | Printer model id. |
| `model` | string | Printer model, for people. |
| `host` | string |  |
| `description` | string or null |  |
| `location` | string or null |  |
| `media` | string or null |  |
| `default` | boolean |  |
| `max_copies` | integer |  |
| `status` | object or null | The state (as PrinterStatus), or state UNREACHABLE with an error; null with status=false. |

### PrinterStatus { #printerstatus }

| Field | Type | Meaning |
|---|---|---|
| `printer` | string |  |
| `host` | string |  |
| `name` | string | Its name in `printers.toml`. |
| `model` | string |  |
| `state` | string |  |
| `error` | null or Warning |  |
| `dpi` | number |  |
| `firmware` | string or null |  |
| `modes` | array of string |  |
| `media` | object | What is loaded, as far as the printer tells: tape_mm, remaining_mm, total_mm, pitch_mm, cassette. |

### Preview { #preview }

| Field | Type | Meaning |
|---|---|---|
| `width` | integer |  |
| `height` | integer |  |
| `warnings` | array of Warning |  |
| `png` | string |  |

Example of a job:

```json
{
  "id": "HxIwYqXuV4qYRBM7",
  "state": "done",
  "printer": "cab",
  "printer_name": "it-cab",
  "host": "cab-it.example.com",
  "owner": "inventory-tool",
  "label": "asset-tag.tavletta",
  "printed": 20,
  "total": 20,
  "rows_printed": 20,
  "cancel_requested": false,
  "error": null,
  "remark": null,
  "warnings": [],
  "created": "2026-11-12T15:34:45+00:00",
  "started": "2026-11-12T15:34:45+00:00",
  "finished": "2026-11-12T15:35:04+00:00"
}
```
