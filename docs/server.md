# Print server: REST API and MCP

`tavletta serve` prints labels without a window, for other programs and
for AI clients. It renders with Pango and Cairo like the command line and
needs no display and no GTK. The code is in `tavletta/server/`; the user
guide is `site/docs/server.md`.

The server is built for network printers first: a printer list
(`printers.toml`) names the printers clients may use, by name; named
access tokens with roles (`tokens.toml`) say who may do what. Finding
printers on the network (mDNS) and USB are optional.

```sh
tavletta serve [--host 127.0.0.1] [--port 8790] [--labels-dir DIR] [--mcp]
               [--printers printers.toml | --allow-printer ADDR] [--discover]
               [--tokens tokens.toml | --token-file FILE] [--allow-origin URL]
               [--max-body MB] [--max-labels N] [--trusted-proxy CIDR]
tavletta mcp [--labels-dir DIR] [--printers printers.toml | --allow-printer ADDR]
             [--discover] [--max-labels N]
tavletta token new NAME [--role preview|print|admin] [--tokens FILE]
                        [--printer NAME ... --printers FILE] [--expires DATE]
                        [--description TEXT] [--scrypt | --from-stdin]
```

| Option | Meaning |
|---|---|
| `--host` | address to listen on; `127.0.0.1` by default. Any other address needs a token. A name is resolved once and only its first address is used (`localhost`: usually 127.0.0.1, not ::1 as well) |
| `--port` | `8790` by default |
| `--printers` | `printers.toml`: the printers clients may use, by name; nothing else can be printed on. Not with `--allow-printer` |
| `--discover` | search the network for printers (mDNS); off by default |
| `--tokens` | `tokens.toml`: named access tokens with roles. Not with `--token-file` |
| `--token-file` | one access token for everything (role admin); else the variable `TAVLETTA_TOKEN`; at least 16 characters |
| `--labels-dir` | the only folder label and data files are read from; `~/.local/share/tavletta/labels` by default (made if missing) |
| `--mcp` | MCP (Streamable HTTP) at `/mcp` as well |
| `--allow-origin` | web origin whose pages may call the server (`https://app.example`): CORS preflights are answered and the answers carry `Access-Control-Allow-Origin`; repeatable |
| `--allow-printer` | without a printer list: only these printers, compared with the host of the address (never the whole address). A pattern of only digits, dots and `*` is an address pattern: four IPv4 parts, `*` standing for whole parts (`192.168.1.*`, `10.*`, `*.5`, `*.*`); a network as Python's ipaddress reads it (`192.168.1.0/24`, `2001:db8::/32`) is one too. Address patterns match IP addresses only, a host name if every address it resolves to matches one of them (so `192.168.1.evil.example` is not in `192.168.1.*`, and `*.*` matches `printer.local` only by the addresses it resolves to, not by its name). Any other pattern is a name pattern (`*.local`, `cups.example.com`) and matches host names. The server then prints on the first address the resolver gives (only that one; there is no trying the others), not on the name (a name looked up again could point elsewhere; an `ipps` printer must then accept its IP address). `*` alone allows everything without a lookup. A pattern that can never match is refused at startup: a network that is none (`10.0.0.0/33`), an address pattern with a part over 255 or a leading zero (`010.1.1.1`), a `*` within a part (`10*`), more than four parts, or fewer than four without a `*` (`1.2.3`, `10`); repeatable |
| `--max-body` | largest request body in MB, 4 by default (parsed JSON takes many times its size) |
| `--max-labels` | most labels in one print job, copies times rows; 10 000 by default (413 `too_many_labels`) |
| `--trusted-proxy` | a reverse proxy, best its own address (`172.30.0.10/32`), whose `X-Forwarded-For` names the client: the rate limit, the 8 requests per client and the log use that address (all header lines together, ports taken off; anything that is no address stops the search at the proxy), the proxy itself is not held to 8 connections. `0.0.0.0/0` or `::/0` (everyone) is warned about. With a proxy, publish the server's port nowhere else; repeatable |

Without `printers.toml`, printer settings (address, safety margin, cut,
mode, colour) come from the app's `settings.json`, as for `tavletta print`,
and a request may name any address (limited by `--allow-printer`).

## Printers (printers.toml)

```toml
[printers.shipping]
backend = "cab"                      # printer model: vc500w, cab, system, plugins
host = "cab-shipping.example.com"    # address or printer URI
description = "Shipping labels"
location = "Warehouse, gate 2"
media = "100 x 50 mm"                # what is loaded, for people
default = true                       # when a request names no printer
max_copies = 200                     # copies of each label, at most
safe_mm = 1.5                        # settings as in the app, for this printer

[printers.kitchen]
backend = "vc500w"
host = "192.0.2.50"
cut = "full"                         # as the model offers: full, half, none
mode = "vivid"                       # vivid, color
data_format = "jpeg"
color = { brightness = 0, red = 0, green = 0, blue = 0 }

[printers.inventory]
backend = "cab"
host = "cab-it.example.com"
media = "57 x 32 mm, ribbon"

[printers.inventory.settings]         # the printer settings of the app
method = "transfer"                  # print method: printer, transfer, direct
upside_down = true                   # print turned by 180 degrees (cab: O R)
```

- Names: letters, digits, `.`, `_`, `-`, up to 64 characters. `backend` and
  `host` are required; `cut`, `mode` and `data_format` must be ones the
  model has, `safe_mm` 0–10, colour steps −3 to 3. At most one default.
- Read strictly at startup: an unknown key, a wrong type, an unknown model
  or address, `cut` on a model that does not cut, `color` on a model
  without colour settings, control characters in a text stop the server
  with the file, the key and the problem, for example `printers.toml:
  printers.kitchen.cut: expected one of: full, half, none`. Changes need
  a restart.
- `[printers.<name>.settings]`: the printer settings the app keeps for the
  model, with the same keys and values as in `settings.json`, and used the
  same way (`PrinterSettings`): the model's own settings (cab: `method`,
  `upside_down`; ZPL and TSPL: `dpi`, `media`, `finish`, `darkness`,
  `speed`, `upside_down`; EPL2: `dpi`, `media`, `darkness`, `upside_down`; ESC/POS: `dpi`, `upside_down`; DYMO: `darkness`,
  `upside_down`; NIIMBOT: `media`, `density`), `quality` (Brother QL:
  `standard`, `high`), the extras (P-touch: `chain`, `high_res`,
  `feed_mm`) and `outlines` on a label sheet (system printer). Only what
  the model has: anything else stops the server like an unknown key. A
  setting left out is the model's default (for `method`: as set on the
  printer). Every print to that printer uses them, through the REST API
  and MCP alike.
- With the list, `printer` in a request is a name from it; the label is
  moved to that printer's model as in the app. Without `printer` the
  default printer is used, or the only one; else printing is refused
  (`no_printer`), a preview uses the label's own model. `host` in a
  request is refused (`host_given`): printers are chosen by name only.
  More copies than `max_copies` are refused (`too_many_copies`).
- `GET /printers` lists the printers of the list a token may use, each
  asked for its state at once (`status`; `UNREACHABLE` with the error if it
  does not answer; `?status=false` skips asking). `GET
  /printers/{name}/status` asks one. What a printer answered (or its
  error) is remembered for 5 s for both, one question at a time per
  printer; messages are made for each request in its language.
- `packaging/docker/printers.example.toml` is a starting point.

## Access tokens and roles (tokens.toml)

```toml
[tokens.shipping-pc]
hash = "sha256:6b86b2…"              # never the token itself
role = "print"                       # preview, print or admin; required
id = "3f9c0a1b2c4d"                  # written by token new: finds the entry at once
printers = ["shipping"]              # only these (needs --printers); all by default
expires = 2027-12-31                 # valid through that day; or a date and time
description = "PC at gate 2"
```

| Role | May |
|---|---|
| `preview` | list printers and labels, describe labels, previews, printer state |
| `print` | as `preview`, and print; see and cancel its own jobs |
| `admin` | as `print`, and see and cancel every job |

- `tavletta token new shipping-pc --role print --tokens tokens.toml`
  makes a random token, `tav_<id>_<secret>` (12 hex digits, 43 random
  characters), adds its SHA-256 hash and the id to the file and prints
  the token once. The file is made with mode 600 if new and locked while
  the entry is added (two at once lose none); the result is checked as
  the server reads it before it is written; names are quoted, so a dot in
  one stays part of the name. `--printer` (repeatable, with `--printers`
  to check the names), `--expires 2027-12-31`, `--description`.
  `--from-stdin` takes a token you chose from standard input instead
  (asked for without echo on a terminal; printable ASCII, at least 16
  characters; not printed again, a line says it was added; refused if it
  is in the file already under another name, or if it looks like a
  made one, `tav_` with an id: it would be looked for by that id, which
  no entry has) and keeps it as an scrypt
  hash, which slows down guessing a chosen token;
  `--scrypt` does that for a made one too (random tokens need no slow
  hash). The default file is `~/.config/tavletta/tokens.toml`.
- The file is read strictly: the role is required, the same hash (also an
  scrypt hash copied with its salt) or id under two names is refused (the
  same chosen token hashed twice cannot be seen in the file; `token new`
  refuses it), scrypt parameters must be digits
  within the memory scrypt may use (salt at least 8, hash 16 to 64
  bytes). A file every user may change is refused, one others may read
  is warned about.
- A token with an id is checked against the entry with that id only;
  entries with an id are never tried for other tokens. A token with an id
  no entry has is compared with a dummy entry too, so the answer takes as
  long as for a known id. A token without an id is compared with the
  entries without an id: SHA-256 first, then scrypt one after the other,
  at most two scrypt checks at once. An id that is not 12 hexadecimal
  digits is warned about and ignored (the token still works, found the
  slow way). A known token whose entry changed is looked up again.
  `token new` checks the file's mode as the server does, also of an empty
  file.
- Revoking: remove the entry. The server reads the file again when it
  changes (inode, time or size; checked with every request) and after
  `SIGHUP` (the handler only sets a flag, installed before the server
  starts; the next request reads). A file
  that became unusable lets no token in until it is fixed, so a revoked
  token never stays valid because of a typo elsewhere; the error is
  logged. Tokens known before stay known when their entry is unchanged.
- An expired token is refused like a wrong one (401). A job of another
  token is "not found" for a `print` token (404); a `preview` token gets
  403 (`forbidden`) for print and job requests and for a `host` of its
  own; a printer a token may not use is "not found" (404), like one that
  does not exist, so the names of the others stay unknown. Without
  `printer` in a request, the default among the printers the token may
  use is chosen, or the only one.
- `--tokens` without `--printers` (or `--allow-printer`) is warned about at
  startup: `print` tokens can then print on any address.
- Each job keeps the name of its token (`owner`); the log names the token
  with every request and job, never the token itself.
- `--token-file` (or `TAVLETTA_TOKEN`) stays for simple setups: one token,
  role admin, named `token`. Without any token (loopback only) requests
  count as `local`, role admin.

## REST API

JSON in and out under `/api/v1`. The full description is served at
`/api/v1/openapi.json` (OpenAPI 3.1, `tavletta/server/openapi.py`), to a
token of any role, like the list of labels (it shows nothing a `preview`
token cannot see otherwise, but the version). `docs/openapi.yaml` and the
reference page `site/docs/api.md` are written from it by
`tools/openapi_docs.py`; a test fails when they are out of date.

| Request | Answer |
|---|---|
| `GET /health` | `{"status": "ok", "version": …}`; the only request without a token |
| `GET /openapi.json` | the OpenAPI description |
| `GET /printers?status=false` | with `printers.toml`: its printers the token may use, with `name`, `printer` (model), `host`, `description`, `location`, `media`, `default`, `max_copies` and `status` |
| `GET /printers?discover=false` | without it: printer models: id, name, kind (`tape`, `labels`, `pages`), resolution, tape widths or label sizes, the address set in the app (`configured_host`) and, with `--discover`, the printers found on the network (`found`; remembered for 30 s) |
| `GET /printers/{name}/status` | the printer's state: `state`, `error` (code and message), `dpi`, `modes`, `media` (tape, remaining, label pitch, cassette). Without a printer list `{id}` is a model and `?host=` the address (default: set in the app). 502 if the printer cannot be reached |
| `GET /labels` | label files in the labels folder and below: `name`, `bytes`, `modified` |
| `GET /labels/{name}` | elements with their names (what `set` fills in), `variables`, `builtin_variables`, `columns` of the placeholders, `data_file` (if usable), size, counter, features |
| `POST /preview` | PNG of the label filled in; `"format": "json"` gives width, height, warnings and the PNG in base64; `"view": "printer"` the image as the printer gets it. Warning codes also in `X-Tavletta-Warnings` |
| `POST /print` | 202 and the job (`Location: /api/v1/jobs/{id}`); 200 if it finished within `wait` seconds |
| `GET /jobs/{id}` | `state` (`queued`, `waiting` for the label file, `printing`, `done`, `failed`, `canceled`), `printed` of `total` labels, `rows_printed`, `printer_name`, `owner`, `error` (code, message), `remark`, `warnings`, times |
| `DELETE /jobs/{id}` | cancel: a waiting job at once, a printing one after the label being printed. 409 if it has finished |

Fields of `POST /preview` and `POST /print` (unknown fields are refused):

| Field | |
|---|---|
| `label` | name in the labels folder, `.tavletta` may be left out; subfolders with `/` |
| `label_content` | or the label itself: the JSON of an `.tavletta` file, checked like a file that is opened. Its data file is ignored, its counter never saved |
| `printer` | with `printers.toml` the printer's name; else a printer model id, if not the label's own (the label is fitted as in the app) |
| `set`, `set_literal` | `{"name": "value"}` as `--set` and `--set-literal`: element names, variables, `date` |
| `counter_start` | counter of the first label |
| `data_rows` | serial labels: `[{"Column": "value"}, …]`; numbers and `null` are taken as text and an empty cell |
| `data_file`, `sheet` | or a CSV, ODS or XLSX file in the labels folder. Without both, the label's own data file is used if it lies beside the label in the labels folder |
| `row` | preview only: the data row shown, from 1 |
| `host`, `copies`, `rows`, `no_save_counter`, `wait` | print only: address, only without `printers.toml` (default: set in the app, else with `--discover` the only printer found), copies of each label (1–9999, or the printer's `max_copies`), data rows (`3-10`, `5`, `7-`, `-4`), do not save the counter, seconds to wait for the job (0–300) |

Errors are `{"error": {"code": "…", "message": "…"}}`. The code is stable
(`label_not_found`, `invalid_field`, `label_unprintable`, `printer_jam`, …);
the message is translated into the language of `Accept-Language` (the
first one there is a catalog for; English ends the list). A failed job
keeps its cause, so `GET /jobs/{id}` translates it for each request.

Checks are those of `tavletta print`: an empty label, unknown or unclosed
variables, dates out of range, elements or images that cannot be drawn,
placeholders without a column and codes a row cannot make are refused
(422) before anything is queued; other warnings are returned with the job.

Example, with the example labels of `docs/examples` as the labels folder
(`serial.tavletta` has the placeholders `{Code}`, `{Name}`, `{Shelf}` and
`{Box}`):

```sh
TOKEN=$(cat ~/.config/tavletta/token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/api/v1/labels/serial
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "serial", "data_rows": [{"Code": "B-07-1", "Name": "Tea", "Shelf": "B-07", "Box": "1"}]}' \
     -o preview.png http://127.0.0.1:8790/api/v1/preview
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "serial", "wait": 60, "data_rows": [
          {"Code": "B-07-1", "Name": "Tea", "Shelf": "B-07", "Box": "1"},
          {"Code": "B-07-2", "Name": "Rice", "Shelf": "B-07", "Box": "2"}]}' \
     http://127.0.0.1:8790/api/v1/print
```

`set` fills in elements by their name (an element named in the app) and
variables of your own (`{=lot}`); `GET /labels/{name}` lists both.

An inventory tool printing one asset label: the label as a file in the
labels folder (`"label": "asset-tag"`) or sent along (`label_content`, the
JSON of a label file), the asset's fields as one data row, the printer by
its name in `printers.toml`, and the number for `{=counter}` if the label
has one:

```sh
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "asset-tag", "printer": "it-cab", "copies": 1,
          "data_rows": [{"AssetNumber": "100001", "Location": "Store 2"}],
          "wait": 30}' \
     http://127.0.0.1:8790/api/v1/print
```

The answer is the job (`state` `done` once printed within `wait`, else
look it up with `GET /jobs/{id}`). `{AssetNumber}` in the label takes the
row's value, in either layout. The walkthrough for such a tool, with
series, retries and code in Python and C#: `site/docs/api-guide.md`; every
request and error code: `site/docs/api.md` and `docs/openapi.yaml`.

`{=counter}`: the label file keeps the number of the next label and the
server saves it after printing (not for `label_content`, nor with
`no_save_counter`). For a tool that keeps its own numbers, put the number
into the data row (`{AssetNumber}` in the label) or send `counter_start`
(with `no_save_counter`): one source of truth, and two servers or a
restored backup cannot hand out a number twice.

## Print jobs

- A job is checked when it is asked for and queued for its printer
  (backend and address). Each printer prints one job at a time; jobs of
  different printers run side by side. At most 100 jobs wait or print at
  once (429 above that); finished jobs are kept for 24 hours (at most
  1000).
- Printing goes through `job.py` and `serial.py`, as in the window and on
  the command line: per-copy variables render each copy, serial labels are
  a job per row.
- The counter: the label file is held (a lock per file in the server and
  the file lock of `model.locked()`, which the command line uses as well)
  from reading the counter when the job starts until the next value is
  saved, also after a printer error. The date of `{=date}` is the moment
  the job starts, unless `date` is set. A file whose counter could not be
  saved is refused before printing (409), as on the command line.
- Cancel is looked at between two labels: between copies rendered one by
  one and between data rows. Copies the printer makes of one image cannot
  be stopped.
- `SIGTERM` (systemd, `docker stop`): waiting jobs are canceled, a printing
  one stops after its label and saves its counter.

## MCP

The same abilities as tools for AI clients (Model Context Protocol):

| Tool | |
|---|---|
| `list_printers` | `GET /printers` |
| `printer_status` | `GET /printers/{id}/status` |
| `list_labels` | `GET /labels` |
| `describe_label` | `GET /labels/{name}` |
| `preview_label` | the image as `image/png` content, warnings as text and `structuredContent` |
| `print_label` | `POST /print`; waits 10 s for the job unless `wait` says otherwise |
| `job_status` | `GET /jobs/{id}`, optionally waiting (`wait`) |
| `cancel_job` | `DELETE /jobs/{id}` |

Arguments are the fields of the REST API. Results carry the JSON as text
and as `structuredContent`; API errors are tool results with `isError` and
the translated message (stdio: the system language). Over HTTP the tools
act with the request's token: `tools/list` leaves out `print_label`,
`job_status` and `cancel_job` for a `preview` token, and a session belongs
to the token that opened it (another token gets 404). stdio counts as
`local` (admin); `--printers` works there too.

Protocol (`tavletta/server/mcp.py`, no dependency):

- JSON-RPC 2.0, one message per request (no batches).
- Both eras of the protocol: `2026-07-28` (stateless: `_meta` carries
  `io.modelcontextprotocol/protocolVersion` and `clientCapabilities` on
  every request, `server/discover`, results with `resultType`) and the
  `initialize` handshake of `2025-11-25` and `2025-06-18` (the requested
  version if supported, else `2025-11-25`). `2025-03-26` is not offered:
  it needs JSON-RPC batches.
- `ping`, `tools/list`, `tools/call`; `notifications/initialized` is
  accepted, `notifications/cancelled` ends the wait of `print_label` or
  `job_status` (the job goes on; `cancel_job` stops it). No resources,
  prompts or sampling.
- **stdio** (`tavletta mcp`): one JSON message per line; logs on stderr,
  stdout carries nothing else. Tool calls run in up to four threads.
- **Streamable HTTP** (`tavletta serve --mcp`, `/mcp`): POST answers with
  `application/json` (no SSE stream), notifications with 202. GET is 405.
  Handshake clients get an `Mcp-Session-Id` (24 random bytes) with the
  initialize answer and must send it afterwards (400 without, 404 for an
  unknown or expired session, one hour unused); DELETE ends it. Modern
  requests need `MCP-Protocol-Version`, `Mcp-Method` and, for
  `tools/call`, `Mcp-Name` (Base64 form decoded) matching the body (400,
  `-32020` otherwise); an unsupported version is 400 with `-32022` and
  the supported versions, an unknown method 404.
- The same token, Origin and Host checks as the REST API.

Why not the official `mcp` package: it brings pydantic, starlette,
uvicorn, anyio, httpx and more into a program that needs eight tools and
two transports; the standard library is enough and keeps the server's
dependencies those of the app.

### Claude Code

```sh
claude mcp add tavletta -- tavletta mcp --labels-dir ~/Labels
# or over HTTP, to a running `tavletta serve --mcp`:
claude mcp add --transport http tavletta http://127.0.0.1:8790/mcp \
    --header "Authorization: Bearer $(cat ~/.config/tavletta/token)"
```

### Claude Desktop

`claude_desktop_config.json` (Linux: `~/.config/Claude/`):

```json
{
  "mcpServers": {
    "tavletta": {
      "command": "tavletta",
      "args": ["mcp", "--labels-dir", "/home/me/Labels"]
    }
  }
}
```

With the Flatpak: `"command": "flatpak"`, `"args": ["run",
"--command=tavletta", "xyz.joshuahirsig.Tavletta", "mcp", "--labels-dir",
"/home/me/Labels"]` (the folder must be visible in the sandbox).

## Security

- **Listening**: `127.0.0.1` by default. A host name is resolved once
  and the server binds that address (no second lookup that could change).
  Another address than loopback is refused without a token; behind a
  reverse proxy with TLS for access from other computers (the server
  speaks plain HTTP).
- **Token**: `Authorization: Bearer`, compared with
  `hmac.compare_digest` (named tokens: their SHA-256 or scrypt hash; a
  token found once is remembered by its SHA-256 until the file changes).
  Tokens must be printable ASCII. Ten wrong tokens from one address (an
  IPv6 address by its /64 network, a link-local one by itself) within a minute make further wrong
  ones 429 until a minute after the tenth; the 429s are not counted. A
  valid token always works: a blocked address still has its SHA-256 and
  known tokens checked, only the slow scrypt check of tokens not seen
  before is skipped. Behind a reverse proxy all clients share the
  proxy's address unless it is given with `--trusted-proxy`. A token file
  readable by others is warned about.
  Without a token (loopback only) every local program can print.
- **Web pages**: on a loopback address, requests whose `Host` is not
  exactly a loopback name with an optional port (`localhost`,
  `127.0.0.1`, `[::1]`; no user name, nothing after it) are refused
  (421), against DNS rebinding. A request with an `Origin` is refused
  (403) unless it is the server's own or allowed with `--allow-origin`.
  POST (also to `/mcp`) needs `Content-Type: application/json` (415), so
  a page cannot send one without a CORS preflight, which is answered only
  for allowed origins. Without a token, a page could still make the
  browser load `GET /printers/{id}/status` as an image, so requests that
  contact printers need the header `X-Tavletta-Request` (any value; 403
  without), which a page cannot add.
- **Printers on the network (SSRF)**: the server connects to printer
  addresses, which is its job. Without `printers.toml`, a client with
  access can make it connect to any address it names (ports 9100, 631 …
  of other hosts in its network). Use `printers.toml` (or at least
  `--allow-printer`) for anything but a single-user setup. Addresses with
  a user name or password (`user@host`) are refused, and answers never
  show them (an address set in the app is shown without them).
- **Files**: names are relative to the labels folder; absolute paths,
  `..`, hidden files and folders, backslashes and NUL are refused, and the
  real path (links followed) must lie inside the folder, so a link to a
  file elsewhere is "not found". Label files end in `.tavletta`, data files
  are tables (`.csv`, `.tsv`, `.txt`, `.ods`, `.xlsx`). A label's own data
  file is used only if it lies beside the label inside the folder; a label
  sent as content never reads a file.
- **Input**: request bodies need a `Content-Length` (ASCII digits) of at
  most 4 MB (`--max-body`; no chunked bodies), bodies over 512 KB are
  parsed two at a time; labels are checked by the model (`Label.from_dict`,
  sizes clamped); data rows and data files keep the limits of
  `tavletta/merge/limits.py` (100 000 rows, 1000 columns, 2 000 000 cells);
  values up to 100 000 characters, 1000 `set` entries, 9999 copies.
- **Image files**: only PNG, JPEG, GIF, BMP, WebP and TIFF are read (EPS
  would run Ghostscript, ICO and ICNS are decoded before their size is
  known). A file whose header says it unpacks to more than 80 megapixels
  is refused when it is opened, before it is decoded; Pillow's own check
  is turned off (a warning filter is not safe in threads). The EXIF
  orientation is applied when the image is decoded, and the size is
  checked again afterwards.
- **Codes**: QR codes and every other symbology come from Zint
  (`zint-bindings`; QR from segno if Zint is missing) as a matrix of
  modules, which the renderer draws with every module the same number of
  whole printer dots, centred in its place; barcodes are therefore a
  little wider than the 1.0 renderer drew them. A linear barcode whose
  modules are thinner than 0.17 mm is drawn and warned about
  (`barcode_narrow`), one of a single printer dot is `code_thin`: it may
  not scan. A Code 128 holds at most 200 characters; a longer one is
  refused for printing in the window, on the command line and by the
  server.
- **Shipping-label PDF pages**: pages in `label_content` are refused
  (422 `pages_not_allowed`) unless the server runs with
  `--allow-pdf-pages`; then PDFs are drawn by Poppler in a separate,
  limited process (see "Document pages" below). The Docker image has
  Poppler; a native install needs `gir1.2-poppler-0.18` / `poppler-glib`.
- **Fonts**: a text whose font fontconfig does not have is drawn in another
  one and warned about (`font_missing`, in the preview and in the job);
  generic names (`Sans`, `Serif`, `Monospace`) always count. See the
  installation guide (`site/docs/server-install.md`) for the fonts of the
  image and how to add more.
- **Rendering**: a label of a request has at most 200 elements, 5000
  characters and 100 lines in each text or code (filled in, and in the
  cells of the columns it uses), then at most 50 images and codes (each
  takes time to draw); more is 413 (`label_too_large`), the first of
  these limits that is broken is told. Within limits:
  - no image made while rendering may exceed 40 megapixels (a text
    block, a decoded image file, the label) and the label not the
    printer's longest length, checked before anything large is drawn;
  - the elements of a label together stay within these 40 megapixels as
    they are drawn;
  - the image files of a label (each counted once) may unpack to at most
    20 megapixels together, checked from their headers before any is
    decoded;
  - each image file is decoded once per render, nothing is kept between
    renders (the window keeps a few in caches).

  Elements are measured first and drawn at their final size (a text
  fitted to a page is not drawn large and scaled down). An element over
  the limits refuses the label (413), it is not drawn as a crossed-out
  box. Every render takes one of two render slots: previews, checks
  before printing and the renders of print jobs, which wait their turn.
  A REST preview or print request is parsed only once it has a slot, so
  waiting requests hold their body, not the parsed label; the printer of
  a print request is looked up after the slot is given back (a name
  lookup or a search can take seconds). An MCP request over HTTP with a
  body over 512 KB is parsed in a render slot too; a smaller one within
  the two-at-a-time parse limit, its tool takes a slot when it renders;
  `print_label` and `job_status` give the slot back while they wait for
  the job. stdio (`tavletta mcp`) has one client, the process's own: its
  lines are parsed as they come. With eight requests waiting, the next
  one gets 503 (`busy`) at once, as one that waits 20 s. The server draws
  a small text in its main thread before it serves: PyGObject sets up
  Pango on first use, which is not safe in several threads at once (the
  window renders in the main thread, where GTK has done it). Labels
  scaled down to a fixed length or page are drawn at their final size and
  look a little sharper than before; others render exactly as before.
- **Roles**: see [tokens.toml](#access-tokens-and-roles-tokenstoml). The token's name, never the token,
  is logged and kept with each job.
- **Printers**: with `printers.toml` clients choose by name and cannot
  make the server connect anywhere else; per token the printers can be
  limited. Without it an address is printable ASCII without spaces, at
  most 512 characters, and `--allow-printer` limits the addresses a client
  may make the server connect to (the backends talk to whatever address
  they are given, as in the app). The network is searched only with
  `--discover`.
- **Resources**: at most 32 connections and requests at once (503 `busy`
  beyond); when all are taken, four more connections are let in that may
  only ask for `/health` and have 1 s to send it, so a stuck server still
  answers its health check. Clients that open slow connections can still
  keep these four busy for a second each; a health check that fails may
  simply be tried again. At most 8 connections from one address (an
  IPv6 address by its /64 network); no thread to be had is a 503 too. 30 s
  to send the whole request: a watchdog stops reading at the deadline
  however the client trickles (inside a line, a header or the body), and
  what was read then is not taken as a request (408 or the connection
  closed); a body the client stops sending is 400, two `Content-Length`
  headers are 400; the answer then has 60 s of its own. Jobs: 100
  waiting or printing, 20 of them for one named token (of tokens.toml;
  one shared token or none is everyone, only the 100 apply), about 256
  MB of requests held by waiting jobs, 10 000 labels per job; a worker
  per printer ends after 60 s without jobs, at most 16 printers at once;
  a new printer takes the place of the one idle longest (429
  `queue_full` beyond any of these). A finished job keeps only its
  outcome, not the request. MCP sessions: 10 per named token (its own
  oldest makes room), 100 in all; only a token's own session ever makes
  room, a full table of others' sessions is 503. MCP bodies are parsed
  within the two-at-a-time limit too. A trusted proxy's requests without
  a usable X-Forwarded-For are held to the overall limits only, not to
  the 8 per client. The systemd unit and the Compose file limit
  memory (1 GB) and processes, and `MALLOC_ARENA_MAX=2` keeps threads
  from holding on to memory a large label used.
- **Logs** (stderr, the journal): client address, token name (`-` before
  a token was accepted), method, path without the query (`-` for both if
  the request line could not be read), status; for each job its id, token name, label,
  printer and outcome. Never headers, tokens or label content.
- No shell is run and no path from a request is used outside the labels
  folder.

### Document pages (`--allow-pdf-pages`)

A label sent along (`label_content`) may not hold document pages
(`page` elements: a carrier's PDF or an image) unless the server runs
with `--allow-pdf-pages`; otherwise it is 422 (`pages_not_allowed`).
Labels of the labels folder may hold them either way. Pages count as
images towards the 50 images and codes; within the render limits:

- an image page counts towards the 20 megapixels of image files (from
  its header, also a TIFF frame before it is decoded) and is decoded once
  per render, never kept; only PNG, JPEG, GIF, BMP, WebP and TIFF are
  read, in the window too (never EPS, which would run Ghostscript);
- a PDF of at most 10 MB is read and drawn by Poppler in a child process
  (`tavletta/shipping/pdfchild.py`) limited to 10 s of processor time,
  20 s in all and an address space of 512 MB plus 24 bytes per pixel of
  the drawing; one that breaks these limits refuses the label (413), a
  damaged one or a crash of the child is a warning (`page_unreadable`).
  Nothing of it is kept in the server's process.

Each page drawn starts a child (about 0.1 s, a black-and-white printer
two), so the option is off by default.

## Server build

- `packaging/docker/Dockerfile`: Debian slim with its security updates,
  Python, PyGObject, Pango and Cairo (no GTK), Poppler for the PDF pages
  of shipping labels (about 35 MB with its libraries), the app in a venv,
  user `tavletta` (uid 10001), `MALLOC_ARENA_MAX=2`, a `HEALTHCHECK` on
  `/api/v1/health`, `LICENSE` and `THIRD_PARTY_NOTICES.md` in
  `/usr/share/doc/tavletta`. No volumes: mount the labels at `/labels` and `printers.toml`, `tokens.toml` (and, if wanted,
  `tavletta/settings.json`) at `/config` (`XDG_CONFIG_HOME`), readable by
  whatever user the container runs as. It listens on all interfaces of
  the container, so it does not start without a token.
- `packaging/docker/compose.yaml`: the published image
  `ghcr.io/joshii-h/tavletta:1.1` ([Container image](#container)), network
  printers, running as the owner of `config/` and `labels/` (`user:`, `TAVLETTA_UID` and
  `TAVLETTA_GID`, 1000 by default), 1 GB memory, 128 processes. `config/` with
  `printers.toml` and `tokens.toml` mounted read-only as a folder (a single
  mounted file would keep showing the old file after `token new` replaced
  it), port on `127.0.0.1` only, read-only file system, no capabilities,
  no discovery.
- `packaging/docker/compose.host.yaml`, an override
  (`-f compose.yaml -f compose.host.yaml`): the host's network
  (`network_mode: host`, listening on 127.0.0.1), `--discover` with the
  host's D-Bus socket for mDNS through Avahi, `/dev/usb` for backends that
  talk USB themselves, with the groups `lp` and `dialout`. CUPS queues of the host (USB printers set up there
  too) are at `ipp://localhost:631/printers/<queue>`; name them in
  `printers.toml`.
- `packaging/docker/compose.build.yaml`, an override
  (`-f compose.yaml -f compose.build.yaml up -d --build`): builds the image
  from the checkout as `tavletta-server` instead of pulling it.
- `packaging/docker/smoke-test.sh IMAGE`: starts the image as
  `compose.yaml` does (read-only, no capabilities, not root) with a
  throwaway token, asks for `/health` and a preview of
  `docs/examples/shelf.tavletta` and waits for the healthcheck.
- `packaging/systemd/tavletta.service`: a user service on `127.0.0.1:8790`
  with MCP and a token file; it makes the labels and config folders,
  then runs sandboxed: the system read-only (`ProtectSystem=strict`), the
  home folder read-only (`ProtectHome=read-only`) but for those two
  folders, `~/.ssh` and `~/.gnupg` not visible, no capabilities, only the
  system calls of ordinary services (`@system-service`), kernel
  settings, modules, logs, clock, host name and cgroups protected, other
  processes invisible, no namespaces, only Internet, Unix and netlink
  sockets, `MemoryMax=1G`, `TasksMax=128`, `MALLOC_ARENA_MAX=2`.
  `systemctl --user reload tavletta` reads tokens.toml again (without
  tokens.toml the reload does nothing, the server goes on). The unit is
  `Type=notify`: the server tells systemd it is ready once it listens, so
  a reload is not sent while it starts; `tavletta serve` also ignores
  SIGHUP before it imports anything (the first thing the `tavletta`
  launcher does) until its handler is installed. The unit and the
  package must be updated together: a unit of `Type=notify` with an
  older Tavletta that never says it is ready is stopped after the start
  timeout, and an older unit with a newer Tavletta loses the readiness
  (the notification is ignored, nothing breaks).

```sh
cd packaging/docker
mkdir -p config labels && cp printers.example.toml config/printers.toml
docker compose pull
docker run --rm --user "$(id -u)" -v ./config:/config --entrypoint tavletta \
    ghcr.io/joshii-h/tavletta:1.1 token new shipping-pc --role print \
    --tokens /config/tokens.toml --printers /config/printers.toml
docker compose up -d
```

For a quick try without files:

```sh
docker run -e TAVLETTA_TOKEN="$(openssl rand -hex 32)" \
    -v ~/Labels:/labels -p 127.0.0.1:8790:8790 ghcr.io/joshii-h/tavletta:1.1
```

## Container image { #container }

`.github/workflows/docker.yml` builds the image on GitHub (the push mirror)
for `linux/amd64` and `linux/arm64` and publishes it as
`ghcr.io/joshii-h/tavletta`:

| Tag | |
|---|---|
| `1.1.0` | a release, from its tag `v1.1.0` |
| `1.1` | the latest bug fix release of 1.1 (what `compose.yaml` uses) |
| `latest` | the latest release (not a pre-release) |
| `edge` | the state of `main`, not a release |

```sh
docker pull ghcr.io/joshii-h/tavletta:1.1.0
```

Before anything is pushed the amd64 image is started with
`packaging/docker/smoke-test.sh` and scanned with Trivy; a release with a
fixable critical or high vulnerability is not published. Pull requests and
manual runs build without pushing. Each pushed image has an SBOM and SLSA
provenance (BuildKit), a GitHub build provenance attestation and a keyless
cosign signature. To check that an image was built by this workflow from
this repository:

```sh
gh attestation verify oci://ghcr.io/joshii-h/tavletta:1.1.0 --repo joshii-h/tavletta
cosign verify ghcr.io/joshii-h/tavletta:1.1.0 \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    --certificate-identity-regexp '^https://github\.com/joshii-h/tavletta/\.github/workflows/docker\.yml@refs/tags/v'
docker buildx imagetools inspect ghcr.io/joshii-h/tavletta:1.1.0 --format '{{ json .SBOM }}'
```

## Roadmap

Planned for the server, not there yet:

- **Single sign-on**: OpenID Connect (Keycloak and others) as a second way
  to authenticate, roles from token claims mapped onto preview, print and
  admin, per-user printer limits.
- **Audit log**: who printed what, where and when, as JSON lines in a file
  of its own, kept apart from the operational log.
- **Metrics**: Prometheus at `/metrics` (jobs by state and printer, labels
  printed, queue length, printer state, request durations), behind its own
  token or role.
