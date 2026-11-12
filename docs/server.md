# Print server: REST API and MCP

`etiketto serve` prints labels without a window, for other programs and
for AI clients. It renders with Pango and Cairo like the command line and
needs no display and no GTK. The code is in `etiketto/server/`; the user
guide is `site/docs/server.md`.

The server is built for network printers first: a printer list
(`printers.toml`) names the printers clients may use, by name; named
access tokens with roles (`tokens.toml`) say who may do what. Finding
printers on the network (mDNS) and USB are optional.

```sh
etiketto serve [--host 127.0.0.1] [--port 8790] [--labels-dir DIR] [--mcp]
               [--printers printers.toml | --allow-printer ADDR] [--discover]
               [--tokens tokens.toml | --token-file FILE] [--allow-origin URL]
etiketto mcp [--labels-dir DIR] [--printers printers.toml | --allow-printer ADDR] [--discover]
etiketto token new NAME [--role preview|print|admin] [--tokens FILE]
                        [--printer NAME ... --printers FILE] [--expires DATE]
                        [--description TEXT] [--scrypt]
```

| Option | Meaning |
|---|---|
| `--host` | address to listen on; `127.0.0.1` by default. Any other address needs a token. |
| `--port` | `8790` by default |
| `--printers` | `printers.toml`: the printers clients may use, by name; nothing else can be printed on. Not with `--allow-printer` |
| `--discover` | search the network for printers (mDNS); off by default |
| `--tokens` | `tokens.toml`: named access tokens with roles. Not with `--token-file` |
| `--token-file` | one access token for everything (role admin); else the variable `ETIKETTO_TOKEN`; at least 16 characters |
| `--labels-dir` | the only folder label and data files are read from; `~/.local/share/etiketto/labels` by default (made if missing) |
| `--mcp` | MCP (Streamable HTTP) at `/mcp` as well |
| `--allow-origin` | web origin whose pages may call the server (`https://app.example`); repeatable |
| `--allow-printer` | without a printer list: only these printer addresses, `*` matches anything (`*.local`, `ipp://cups/*`); repeatable |

Without `printers.toml`, printer settings (address, safety margin, cut,
mode, colour) come from the app's `settings.json`, as for `etiketto print`,
and a request may name any address (limited by `--allow-printer`).

## Printers (printers.toml) { #printers }

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
mode = "vivid"                       # vivid, color, bw
data_format = "jpeg"
color = { brightness = 0, red = 0, green = 0, blue = 0 }
```

- Names: letters, digits, `.`, `_`, `-`, up to 64 characters. `backend` and
  `host` are required; `cut`, `mode` and `data_format` must be ones the
  model has, `safe_mm` 0–10, colour steps −3 to 3. At most one default.
- Read strictly at startup: an unknown key, a wrong type, an unknown model
  or address stop the server with the file, the key and the problem, for
  example `printers.toml: printers.shipping.cut: expected one of: full,
  half, none`. Changes need a restart.
- With the list, `printer` in a request is a name from it; the label is
  moved to that printer's model as in the app. Without `printer` the
  default printer is used, or the only one; else printing is refused
  (`no_printer`), a preview uses the label's own model. `host` in a
  request is refused (`host_given`): printers are chosen by name only.
  More copies than `max_copies` are refused (`too_many_copies`).
- `GET /printers` lists the printers of the list a token may use, each
  asked for its state at once (`status`; `UNREACHABLE` with the error if it
  does not answer; `?status=false` skips asking). `GET
  /printers/{name}/status` asks one.
- `packaging/docker/printers.example.toml` is a starting point.

## Access tokens and roles (tokens.toml) { #tokens }

```toml
[tokens.shipping-pc]
hash = "sha256:6b86b2…"              # never the token itself
role = "print"                       # preview, print or admin
printers = ["shipping"]              # only these (needs --printers); all by default
expires = 2027-12-31                 # a date, or a date and time (local time if no zone)
description = "PC at gate 2"
```

| Role | May |
|---|---|
| `preview` | list printers and labels, describe labels, previews, printer state |
| `print` | as `preview`, and print; see and cancel its own jobs |
| `admin` | as `print`, and see and cancel every job |

- `etiketto token new shipping-pc --role print --tokens tokens.toml`
  makes a random token (`etk_` and 43 characters), adds its SHA-256 hash
  to the file (made with mode 600 if new; the file is checked as the
  server reads it before it is written) and prints the token once.
  `--printer` (repeatable, with `--printers` to check the names),
  `--expires 2027-12-31`, `--description`. `--scrypt` stores an scrypt
  hash instead (for tokens people choose; random tokens need no slow
  hash). The default file is `~/.config/etiketto/tokens.toml`.
- Revoking: remove the entry. The server reads the file again when it
  changes (checked with every request) and on `SIGHUP`. A file that
  became unusable lets no token in until it is fixed, so a revoked token
  never stays valid because of a typo elsewhere; the error is logged.
- An expired token is refused like a wrong one (401). A job of another
  token is "not found" for a `print` token (404); a `preview` token gets
  403 (`forbidden`) for print and job requests, a printer it may not use
  403 (`printer_forbidden`).
- Each job keeps the name of its token (`owner`); the log names the token
  with every request and job, never the token itself.
- `--token-file` (or `ETIKETTO_TOKEN`) stays for simple setups: one token,
  role admin, named `token`. Without any token (loopback only) requests
  count as `local`, role admin.

## REST API

JSON in and out under `/api/v1`. The full description is served at
`/api/v1/openapi.json` (OpenAPI 3.1, `etiketto/server/openapi.py`).

| Request | Answer |
|---|---|
| `GET /health` | `{"status": "ok", "version": …}`; the only request without a token |
| `GET /openapi.json` | the OpenAPI description |
| `GET /printers?status=false` | with `printers.toml`: its printers the token may use, with `name`, `printer` (model), `host`, `description`, `location`, `media`, `default`, `max_copies` and `status` |
| `GET /printers?discover=false` | without it: printer models: id, name, kind (`tape`, `labels`, `pages`), resolution, tape widths or label sizes, the address set in the app (`configured_host`) and, with `--discover`, the printers found on the network (`found`; remembered for 30 s) |
| `GET /printers/{name}/status` | the printer's state: `state`, `error` (code and message), `dpi`, `modes`, `media` (tape, remaining, label pitch, cassette). Without a printer list `{id}` is a model and `?host=` the address (default: set in the app). 502 if the printer cannot be reached |
| `GET /labels` | label files in the labels folder and below: `name`, `bytes`, `modified` |
| `GET /labels/{name}` | elements with their names (what `set` fills in), `variables`, `builtin_variables`, `columns` of the placeholders, `data_file` (if usable), size, counter, features |
| `POST /preview` | PNG of the label filled in; `"format": "json"` gives width, height, warnings and the PNG in base64; `"view": "printer"` the image as the printer gets it. Warning codes also in `X-Etiketto-Warnings` |
| `POST /print` | 202 and the job (`Location: /api/v1/jobs/{id}`); 200 if it finished within `wait` seconds |
| `GET /jobs/{id}` | `state` (`queued`, `printing`, `done`, `failed`, `canceled`), `printed` of `total` labels, `rows_printed`, `printer_name`, `owner`, `error` (code, message), `remark`, `warnings`, times |
| `DELETE /jobs/{id}` | cancel: a waiting job at once, a printing one after the label being printed. 409 if it has finished |

Fields of `POST /preview` and `POST /print` (unknown fields are refused):

| Field | |
|---|---|
| `label` | name in the labels folder, `.etiketto` may be left out; subfolders with `/` |
| `label_content` | or the label itself: the JSON of an `.etiketto` file, checked like a file that is opened. Its data file is ignored, its counter never saved |
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

Checks are those of `etiketto print`: an empty label, unknown or unclosed
variables, dates out of range, elements or images that cannot be drawn,
placeholders without a column and codes a row cannot make are refused
(422) before anything is queued; other warnings are returned with the job.

Example:

```sh
TOKEN=$(cat ~/.config/etiketto/token)
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/api/v1/labels/shelf
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "shelf", "set": {"title": "Flour"}}' \
     -o preview.png http://127.0.0.1:8790/api/v1/preview
curl -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
     -d '{"label": "shelf", "data_rows": [{"Name": "Tea"}, {"Name": "Rice"}], "wait": 60}' \
     http://127.0.0.1:8790/api/v1/print
```

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

Protocol (`etiketto/server/mcp.py`, no dependency):

- JSON-RPC 2.0, one message per request (no batches).
- Both eras of the protocol: `2026-07-28` (stateless: `_meta` carries
  `io.modelcontextprotocol/protocolVersion` and `clientCapabilities` on
  every request, `server/discover`, results with `resultType`) and the
  `initialize` handshake of `2025-11-25`, `2025-06-18` and `2025-03-26`
  (the requested version if supported, else `2025-11-25`).
- `ping`, `tools/list`, `tools/call`; `notifications/initialized` and
  `notifications/cancelled` are accepted. No resources, prompts or
  sampling.
- **stdio** (`etiketto mcp`): one JSON message per line; logs on stderr,
  stdout carries nothing else. Tool calls run in up to four threads.
- **Streamable HTTP** (`etiketto serve --mcp`, `/mcp`): POST answers with
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
claude mcp add etiketto -- etiketto mcp --labels-dir ~/Labels
# or over HTTP, to a running `etiketto serve --mcp`:
claude mcp add --transport http etiketto http://127.0.0.1:8790/mcp \
    --header "Authorization: Bearer $(cat ~/.config/etiketto/token)"
```

### Claude Desktop

`claude_desktop_config.json` (Linux: `~/.config/Claude/`):

```json
{
  "mcpServers": {
    "etiketto": {
      "command": "etiketto",
      "args": ["mcp", "--labels-dir", "/home/me/Labels"]
    }
  }
}
```

With the Flatpak: `"command": "flatpak"`, `"args": ["run",
"--command=etiketto", "xyz.joshuahirsig.Etiketto", "mcp", "--labels-dir",
"/home/me/Labels"]` (the folder must be visible in the sandbox).

## Security

- **Listening**: `127.0.0.1` by default. Another address is refused
  without a token; behind a reverse proxy with TLS for access from other
  computers (the server speaks plain HTTP).
- **Token**: `Authorization: Bearer`, compared with
  `hmac.compare_digest` (named tokens: their SHA-256 or scrypt hash; a
  token found once is remembered by its SHA-256 until the file changes). Ten wrong tokens from one address within a minute
  block that address for the minute (429); behind a reverse proxy all
  clients share the proxy's address. A token file readable by others is
  warned about. Without a token (loopback only) every local program can
  print.
- **DNS rebinding**: on a loopback address, requests whose `Host` is not a
  loopback name (`localhost`, `127.0.0.1`, `[::1]`) are refused (421). A
  request with an `Origin` (from a web page) is refused (403) unless it is
  the server's own or allowed with `--allow-origin`; no CORS headers are
  sent, and POST needs `Content-Type: application/json`, so a page cannot
  send one without a preflight that fails.
- **Files**: names are relative to the labels folder; absolute paths,
  `..`, hidden files and folders, backslashes and NUL are refused, and the
  real path (links followed) must lie inside the folder, so a link to a
  file elsewhere is "not found". Label files end in `.etiketto`, data files
  are tables (`.csv`, `.tsv`, `.txt`, `.ods`, `.xlsx`). A label's own data
  file is used only if it lies beside the label inside the folder; a label
  sent as content never reads a file.
- **Input**: request bodies need a `Content-Length` of at most 32 MB (no
  chunked bodies); labels are checked by the model (`Label.from_dict`,
  sizes clamped); data rows and data files keep the limits of
  `etiketto/merge/limits.py` (100 000 rows, 1000 columns, 2 000 000 cells);
  values up to 100 000 characters, 1000 `set` entries, 9999 copies.
- **Roles**: see [tokens.toml](#tokens). The token's name, never the token,
  is logged and kept with each job.
- **Printers**: with `printers.toml` clients choose by name and cannot
  make the server connect anywhere else; per token the printers can be
  limited. Without it an address is printable ASCII without spaces, at
  most 512 characters, and `--allow-printer` limits the addresses a client
  may make the server connect to (the backends talk to whatever address
  they are given, as in the app). The network is searched only with
  `--discover`.
- **Resources**: at most 32 connections at once, 60 s to send a request,
  two previews rendered at once, 100 jobs queued.
- **Logs** (stderr, the journal): client address, token name, method, path
  without the query, status; for each job its id, token name, label,
  printer and outcome. Never headers, tokens or label content.
- No shell is run and no path from a request is used outside the labels
  folder.

## Server build

- `packaging/docker/Dockerfile`: Debian slim with Python, PyGObject,
  Pango and Cairo (no GTK), the app in a venv, user `etiketto` (uid
  10001). Labels in `/labels`, settings in `/data`. It listens on all
  interfaces of the container, so it does not start without a token.
- `packaging/docker/compose.yaml`: network printers. `config/` with
  `printers.toml` and `tokens.toml` mounted read-only as a folder (a single
  mounted file would keep showing the old file after `token new` replaced
  it), port on `127.0.0.1` only, read-only file system, no capabilities,
  no discovery.
- `packaging/docker/compose.host.yaml`, an override
  (`-f compose.yaml -f compose.host.yaml`): the host's network
  (`network_mode: host`, listening on 127.0.0.1), `--discover` with the
  host's D-Bus socket for mDNS through Avahi, `/dev/usb` for backends that
  talk USB themselves. CUPS queues of the host (USB printers set up there
  too) are at `ipp://localhost:631/printers/<queue>`; name them in
  `printers.toml`.
- `packaging/systemd/etiketto.service`: a user service on `127.0.0.1:8790`
  with MCP and a token file, sandboxed (`ProtectSystem=strict`, write
  access only to the labels and the config folder).

```sh
cd packaging/docker
mkdir -p config labels && cp printers.example.toml config/printers.toml
docker compose build
docker run --rm --user "$(id -u)" -v ./config:/config --entrypoint etiketto \
    etiketto-server token new shipping-pc --role print \
    --tokens /config/tokens.toml --printers /config/printers.toml
docker compose up -d
```

For a quick try without files:

```sh
docker run -e ETIKETTO_TOKEN="$(openssl rand -hex 32)" \
    -v ~/Labels:/labels -p 127.0.0.1:8790:8790 etiketto-server
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
