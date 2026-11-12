# Print server

Other programs and AI assistants can print labels through Etiketto without
opening the window: `etiketto serve` offers a web interface for programs
(REST API), `etiketto mcp` connects AI assistants such as Claude.

## Start { #start }

```sh
etiketto serve --labels-dir ~/Labels
```

- The server listens on `http://127.0.0.1:8790`, only on this computer.
- Only label files in the labels folder (and its subfolders) can be used,
  with their data files. Without `--labels-dir`:
  `~/.local/share/etiketto/labels`.
- Printer address, safety margin and print settings are those set in the
  app.
- `--mcp` adds the connection for AI assistants at `/mcp`.
- Stop with Ctrl+C: waiting jobs are canceled, a label being printed is
  finished and the counter saved.

## Access token { #token }

- Create a token and keep it secret:

  ```sh
  mkdir -p ~/.config/etiketto
  openssl rand -hex 32 > ~/.config/etiketto/token
  chmod 600 ~/.config/etiketto/token
  etiketto serve --token-file ~/.config/etiketto/token
  ```

- Every request then needs the header `Authorization: Bearer <token>`.
- To listen on another address (`--host 0.0.0.0`), a token is required.
  For other computers, put a reverse proxy with HTTPS in front.
- `--allow-printer` limits the printer addresses, for example
  `--allow-printer '*.local'`.

## Programs (REST API) { #api }

- The description of all requests: `http://127.0.0.1:8790/api/v1/openapi.json`.
- `GET /api/v1/labels` lists the labels, `GET /api/v1/labels/<name>`
  shows what can be filled in: element names, variables and columns.
- `POST /api/v1/preview` returns the label as a PNG image.
- `POST /api/v1/print` queues a print job; `GET /api/v1/jobs/<id>` shows
  its state, `DELETE` cancels it between two labels.
- Each printer prints one job after the other.
- Messages come in the language of the `Accept-Language` header.

```sh
curl -H "Authorization: Bearer $(cat ~/.config/etiketto/token)" \
     -H 'Content-Type: application/json' \
     -d '{"label": "shelf", "set": {"title": "Flour"}, "copies": 2}' \
     http://127.0.0.1:8790/api/v1/print
```

- `set` fills in elements by their name and variables such as `{=lot}`,
  as on the command line.
- Serial labels: `"data_rows": [{"Name": "Tea"}, {"Name": "Rice"}]`, or
  `"data_file"` with a table in the labels folder.

## AI assistants (MCP) { #mcp }

The assistant can list the labels and printers, show a preview, print and
follow or cancel print jobs. It asks before it uses a tool, depending on
its settings.

Claude Code:

```sh
claude mcp add etiketto -- etiketto mcp --labels-dir ~/Labels
```

Claude Desktop, in `claude_desktop_config.json`:

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

- Over the network: `etiketto serve --mcp` and the address
  `http://127.0.0.1:8790/mcp` with the access token as
  `Authorization: Bearer` header.
- Printing uses real labels: ask for a preview first.

## Server and container { #service }

- As a service of your user: `packaging/systemd/etiketto.service`
  (instructions in the file).
- As a container: `packaging/docker/Dockerfile` and `compose.yaml`. To
  find printers on the network, the container needs the network of the
  computer (`network_mode: host`).
- Technical details and security:
  [docs/server.md](https://git.joshuahirsig.xyz/joshii/etiketto/src/branch/main/docs/server.md).
