# Print server

Other programs and AI assistants can print labels through Tavletta without
opening the window: `tavletta serve` offers a web interface for programs
(REST API), `tavletta mcp` connects AI assistants such as Claude.

## Start { #start }

```sh
tavletta serve --labels-dir ~/Labels
```

- The server listens on `http://127.0.0.1:8790`, only on this computer.
- Only label files in the labels folder (and its subfolders) can be used,
  with their data files. Without `--labels-dir`:
  `~/.local/share/tavletta/labels`.
- Printer address, safety margin and print settings are those set in the
  app.
- `--mcp` adds the connection for AI assistants at `/mcp`.
- Stop with Ctrl+C: waiting jobs are canceled, a label being printed is
  finished and the counter saved.

## Printers { #printers }

- Name the printers in a file `printers.toml` and start the server with
  `--printers printers.toml`. Programs then choose a printer by its name;
  no other printer can be used.

  ```toml
  [printers.shipping]
  backend = "cab"
  host = "cab-shipping.example.com"
  description = "Shipping labels"
  location = "Warehouse, gate 2"
  default = true
  max_copies = 200
  ```

- `backend` is the printer model, `host` the printer's address.
- The printer's settings, as in the app's printer settings, go into a
  table of their own; only the ones the model has:

  ```toml
  [printers.inventory]
  backend = "cab"
  host = "printer.example.com"

  [printers.inventory.settings]
  method = "transfer"
  upside_down = true
  ```

- `method` is the [[ui:Print method]] (`printer`, `transfer`, `direct`),
  `upside_down` [[ui:Print upside down]]; others are `quality`, `chain`,
  `high_res`, `feed_mm`, `darkness`, `media` and `outlines`, see
  [docs/server.md](https://git.joshuahirsig.xyz/joshii/tavletta/src/branch/main/docs/server.md#printers).
- Mistakes in the file are named when the server starts.
- The list of printers shows each printer with its state.
- The server searches the network for printers only when started with
  `--discover`.

## Access token { #token }

- Create a token and keep it secret:

  ```sh
  mkdir -p ~/.config/tavletta
  openssl rand -hex 32 > ~/.config/tavletta/token
  chmod 600 ~/.config/tavletta/token
  tavletta serve --token-file ~/.config/tavletta/token
  ```

- Every request then needs the header `Authorization: Bearer <token>`.
- To listen on another address (`--host 0.0.0.0`), a token is required.
  For other computers, put a reverse proxy with HTTPS in front.
- `--allow-printer` limits the printer addresses, for example
  `--allow-printer '*.local'`.

## Access per device { #tokens }

- Several devices or people get a token each, with a role:

  ```sh
  tavletta token new shipping-pc --role print --tokens ~/.config/tavletta/tokens.toml
  tavletta serve --printers printers.toml --tokens ~/.config/tavletta/tokens.toml
  ```

- The token is shown only once; the file keeps only a fingerprint (hash)
  of it.
- `preview` only lists and shows previews, `print` also prints and
  follows its own jobs, `admin` sees and cancels all jobs.
- `--printer shipping` limits a token to that printer, `--expires
  2027-12-31` to a date.
- To take a token back, delete its entry in `tokens.toml`. The server
  notices it at once.

## Programs (REST API) { #api }

- The description of all requests: `http://127.0.0.1:8790/api/v1/openapi.json`.
- `GET /api/v1/labels` lists the labels, `GET /api/v1/labels/<name>`
  shows what can be filled in: element names, variables and columns.
- `POST /api/v1/preview` returns the label as a PNG image.
- `POST /api/v1/print` queues a print job; `GET /api/v1/jobs/<id>` shows
  its state, `DELETE` cancels it between two labels.
- Each printer prints one job after the other.
- Messages come in the language of the `Accept-Language` header.
- Without an access token, requests that ask printers need the header
  `X-Tavletta-Request: 1`, so that no web page can send them.

```sh
curl -H "Authorization: Bearer $(cat ~/.config/tavletta/token)" \
     -H 'Content-Type: application/json' \
     -d '{"label": "jar", "set": {"title": "Flour"}, "copies": 2}' \
     http://127.0.0.1:8790/api/v1/print
```

- `set` fills in elements by their name and variables such as `{=lot}`,
  as on the command line: here the label `jar` from the labels folder,
  with a text element named `title`.
- Serial labels: `"data_rows": [{"Name": "Tea"}, {"Name": "Rice"}]`, or
  `"data_file"` with a table in the labels folder.
- One label from another program, for example an inventory: the fields
  as one data row, the printer by its name.

  ```sh
  curl -H "Authorization: Bearer $(cat ~/.config/tavletta/token)" \
       -H 'Content-Type: application/json' \
       -d '{"label": "inventory", "printer": "inventory", "copies": 1,
            "data_rows": [{"Inventarnummer": "100001"}]}' \
       http://127.0.0.1:8790/api/v1/print
  ```

- `{=counter}`: the server saves the next number in the label file. A
  program that keeps its own numbers sends the number in the data row or
  as `"counter_start"` with `"no_save_counter": true`.

## AI assistants (MCP) { #mcp }

The assistant can list the labels and printers, show a preview, print and
follow or cancel print jobs. It asks before it uses a tool, depending on
its settings.

Claude Code:

```sh
claude mcp add tavletta -- tavletta mcp --labels-dir ~/Labels
```

Claude Desktop, in `claude_desktop_config.json`:

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

- Over the network: `tavletta serve --mcp` and the address
  `http://127.0.0.1:8790/mcp` with the access token as
  `Authorization: Bearer` header.
- Printing uses real labels: ask for a preview first.

## Server and container { #service }

- As a service of your user: `packaging/systemd/tavletta.service`
  (instructions in the file).
- As a container: `packaging/docker/compose.yaml`, for printers in the
  network, with `printers.toml` and `tokens.toml` in the folder `config`.
- To find printers on the network, or for USB, add `compose.host.yaml`:
  `docker compose -f compose.yaml -f compose.host.yaml up -d`.
- Technical details and security:
  [docs/server.md](https://git.joshuahirsig.xyz/joshii/tavletta/src/branch/main/docs/server.md).
