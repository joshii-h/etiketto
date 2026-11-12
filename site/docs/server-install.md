# Server installation

This guide sets up the print server (`tavletta serve`) on a Linux server
for a company: other programs, for example an inventory tool, print labels
through it over the network. What the server does and how programs use it:
[Print server](server.md) and [API reference](api.md).

## Overview { #overview }

- One small service: a REST API (and MCP for AI assistants) on port 8790.
  It renders the labels itself and sends them to the printers over the
  network; no desktop, no browser, no database.
- The printers it may use are named in `printers.toml`; no other address
  can be printed on.
- Every program or device gets its own access token with a role
  (`tokens.toml`); the file keeps only a fingerprint (hash) of each token.
- The labels are `.tavletta` files in a labels folder, designed in the
  Tavletta app and copied there.
- It listens on `127.0.0.1` only. Other computers reach it through a
  reverse proxy with HTTPS (Traefik, nginx, Caddy).

## Requirements { #requirements }

- A Linux server on x86-64 or ARM64 (a virtual machine or a Raspberry Pi 4/5 is enough).
- Either Docker Engine with the Compose plugin (option A, recommended),
  or Python 3.11 or newer with PyGObject, Pango and Cairo from the
  distribution (option B).
- Resources: 1 CPU core, 1 GB RAM reserved (the server usually uses
  50–150 MB; a large label is limited to 40 megapixels), 500 MB of disk
  for the image or the installation, plus the labels.
- Network from the server to the printers:

  | Printer | Port |
  |---|---|
  | cab, Brother QL, ZPL, EPL2, TSPL, ESC/POS, DYMO and others printing "raw" | TCP 9100 |
  | Office printers and CUPS queues (IPP) | TCP 631 |
  | Brother VC-500W | TCP 9100 |

- Only for finding printers automatically (mDNS) or USB printers: the
  host's network (option A with `compose.host.yaml`) or a native
  installation.
- Fonts: every font the labels use must be installed on the server (see
  [Fonts](#fonts)).

## Option A: Docker Compose { #docker }

### 1. Get the files and the image { #docker-build }

```sh
sudo git clone https://git.joshuahirsig.xyz/joshii/tavletta.git /opt/tavletta
cd /opt/tavletta
sudo git checkout v1.1.0      # the release to run: git tag lists them
cd packaging/docker
sudo docker compose pull      # ghcr.io/joshii-h/tavletta:1.1, for x86-64 and ARM64
```

The image (about 230 MB), built and tested by the project's CI and
signed (see [Server and container](server.md#container)), contains Tavletta, Python, Pango and Cairo,
the fonts DejaVu, Source Sans 3, Liberation (metrics of Arial, Times New
Roman and Courier New) and Carlito (metrics of Calibri). It runs as an
unprivileged user, read-only, without capabilities.

### 2. Folders { #docker-folders }

Everything that belongs to your installation lives in three folders next
to `compose.yaml` (git ignores them):

```text
/opt/tavletta/packaging/docker/
├── compose.yaml
├── config/          printers.toml, tokens.toml
├── labels/          the label files (.tavletta) and their data files
└── fonts/           fonts of your own (.ttf, .otf), may stay empty
```

```sh
sudo mkdir -p config labels fonts
sudo chown -R 1000:1000 config labels fonts
```

The container runs as user 1000 (set `TAVLETTA_UID` and `TAVLETTA_GID`
for another one): it reads `config/` and saves counters into the label
files of `labels/`.

### 3. Printers: printers.toml { #printers }

`config/printers.toml` names every printer programs may use. An example
with a cab printer for asset labels (thermal transfer, printed turned by
180 degrees), a Brother QL at the reception and an office printer over
IPP:

```toml
[printers.it-cab]
backend = "cab"                          # cab EOS, SQUIX, MACH
host = "cab-it.example.com"              # name or IP address of the printer
description = "Asset labels"
location = "IT store room"
media = "57 x 32 mm polyester, resin ribbon"
default = true                           # used when a request names no printer
max_copies = 50

[printers.it-cab.settings]
method = "transfer"                      # thermal transfer with ribbon (direct: without)
upside_down = true                       # printed turned by 180 degrees

[printers.reception-ql]
backend = "brother_ql"                   # Brother QL-700, QL-8xx, QL-1100 …
host = "192.0.2.21"
description = "Visitor and sample labels"
location = "Reception"
media = "DK-22205, 62 mm continuous"
max_copies = 20
cut = "full"

[printers.reception-ql.settings]
quality = "high"

[printers.office]
backend = "system"                       # any IPP printer or CUPS queue
host = "ipp://office-printer.example.com/ipp/print"
description = "Label sheets, A4"
max_copies = 10
```

- `backend` is the printer model: `cab`, `brother_ql`,
  `brother_ql_diecut`, `ptouch`, `vc500w`, `zpl`, `epl`, `tspl`, `escpos`,
  `dymo`, `niimbot`, `system` (`tavletta list-printers` shows them).
- `host` is the printer's name or IP address; for `system` an IPP
  address. Give printers a fixed address (DHCP reservation or DNS name).
- The settings of a model are those of the app's printer settings; the
  full list is in
  [docs/server.md](https://git.joshuahirsig.xyz/joshii/tavletta/src/branch/main/docs/server.md).
- A mistake stops the server with the file, the key and the problem, for
  example `printers.toml: printers.it-cab.settings.method: expected one
  of: printer, transfer, direct`. Changes need a restart.

### 4. Access tokens: tokens.toml { #tokens }

Create one token per program or device. The token is shown once; the
file keeps only its hash.

```sh
sudo docker run --rm --user 1000:1000 -v ./config:/config --entrypoint tavletta \
    ghcr.io/joshii-h/tavletta:1.1 token new inventory-tool --role print --printer it-cab \
    --description "Inventory tool" \
    --tokens /config/tokens.toml --printers /config/printers.toml
```

```text
tav_3f9c0a1b2c4d_Xq2…
tavletta: The token is shown only this once; /config/tokens.toml keeps only its hash.
```

- Give the token to the program's administrator over a safe channel (a
  password manager), never by e-mail.
- Roles: `preview` (look only), `print` (also print, see its own jobs),
  `admin` (see and cancel every job). Give programs `print`, monitoring
  `preview`.
- `--printer` (repeatable) limits the token to these printers;
  `--expires 2027-12-31` to a date.
- Always pass `--printers`: once a token is limited to printers, the file
  can only be checked with the printer list.
- New tokens work at once, without a restart. To revoke one, delete its
  entry from `config/tokens.toml`.

### 5. Labels { #labels }

Design the labels in the Tavletta app and copy the `.tavletta` files
into `labels/` (subfolders are allowed). Programs name them without the
ending: `labels/inventory/asset-tag.tavletta` is
`"label": "inventory/asset-tag"`.

```sh
sudo cp asset-tag.tavletta labels/
sudo chown 1000:1000 labels/asset-tag.tavletta
```

### 6. Start { #docker-start }

```sh
sudo docker compose up -d
sudo docker compose ps
curl http://127.0.0.1:8790/api/v1/health
```

```text
{"status": "ok", "version": "1.1.0"}
```

- The container restarts by itself after a crash or a reboot.
- Set the time zone of `{=date}` and `{=time}` on labels with
  `TAVLETTA_TZ=Europe/Zurich` in a file `.env` next to `compose.yaml`
  (UTC by default).
- Check the token and the printers:

  ```sh
  TOKEN=tav_3f9c0a1b2c4d_Xq2…
  curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8790/api/v1/printers
  ```

  Each printer is listed with its state; `UNREACHABLE` with a message if
  the server cannot reach it.

### Host network: mDNS and USB { #docker-host }

To find printers on the network by mDNS, to print on CUPS queues of the
host or on USB printers, start with the override `compose.host.yaml`:

```sh
sudo docker compose -f compose.yaml -f compose.host.yaml up -d
```

The container then shares the host's network and listens on
`127.0.0.1:8790` of the host. CUPS queues of the host are
`ipp://localhost:631/printers/<queue>` in `printers.toml`.

### Logs { #docker-logs }

```sh
sudo docker compose logs -f
```

Each request is logged with the client's address, the token's name, the
path and the status; each job with its label, printer and outcome. Tokens
and label contents are never logged.

### Updating { #docker-update }

```sh
cd /opt/tavletta
sudo git fetch --tags
sudo git checkout v1.2.0         # the new release
cd packaging/docker
sudo docker compose pull
sudo docker compose up -d
```

Read the release notes first. Waiting jobs are canceled when the server
stops; a label being printed is finished. `config/`, `labels/` and
`fonts/` are kept.

### Backup { #docker-backup }

Back up the three folders: `config/` (the printer list, the token
hashes), `labels/` (the labels and their counters: the next number of
each label with `{=counter}` is saved in its file) and `fonts/`. The
image can be pulled again at any time.

### Uninstalling { #docker-uninstall }

```sh
cd /opt/tavletta/packaging/docker
sudo docker compose down
sudo docker image rm ghcr.io/joshii-h/tavletta:1.1
sudo rm -r /opt/tavletta     # also deletes config, labels and fonts
```

## Option B: native with systemd { #native }

For a server without Docker. Tested on Debian 13, Ubuntu 24.04 and
Fedora 44. The installation needs internet access (Python packages from
PyPI, one from GitHub).

### 1. System packages { #native-packages }

Debian, Ubuntu:

```sh
sudo apt install python3 python3-venv python3-gi python3-gi-cairo gir1.2-pango-1.0 \
    gettext git fontconfig fonts-dejavu-core
```

Fedora:

```sh
sudo dnf install python3 python3-gobject gobject-introspection pango gettext git-core
```

No GTK is needed. `gettext` and `git` are needed only while installing.

### 2. Install Tavletta { #native-install }

Into its own Python environment in `/opt/tavletta`, which uses the
system's PyGObject:

```sh
sudo python3 -m venv --system-site-packages /opt/tavletta
sudo /opt/tavletta/bin/pip install "git+https://git.joshuahirsig.xyz/joshii/tavletta.git@v1.1.0"
/opt/tavletta/bin/tavletta --version
```

### 3. Service user and folders { #native-user }

```sh
sudo useradd --system --home-dir /var/lib/tavletta --create-home \
    --shell /usr/sbin/nologin tavletta
sudo install -d -o tavletta -g tavletta -m 0750 /etc/tavletta /var/lib/tavletta/labels
```

| Path | Content | Owner, mode |
|---|---|---|
| `/etc/tavletta/printers.toml` | the printers ([as above](#printers)) | `tavletta`, 600 |
| `/etc/tavletta/tokens.toml` | the token hashes | `tavletta`, 600 |
| `/var/lib/tavletta/labels/` | the label files | `tavletta`, 750 |
| `/usr/local/share/fonts/` | fonts of your own | `root`, readable by all |

Create the tokens as the service user, so the file belongs to it:

```sh
sudo -u tavletta /opt/tavletta/bin/tavletta token new inventory-tool --role print \
    --printer it-cab --tokens /etc/tavletta/tokens.toml --printers /etc/tavletta/printers.toml
```

The server refuses a `tokens.toml` that every user may change, and warns
about one that others may read.

### 4. The service { #native-service }

`packaging/systemd/tavletta-server.service` runs the server as
`tavletta`, sandboxed (read-only system, no home folders, no
capabilities, 1 GB memory):

```sh
sudo curl -fsSLo /etc/systemd/system/tavletta-server.service \
    https://git.joshuahirsig.xyz/joshii/tavletta/raw/tag/v1.1.0/packaging/systemd/tavletta-server.service
sudo systemctl daemon-reload
sudo systemctl enable --now tavletta-server
systemctl status tavletta-server
curl http://127.0.0.1:8790/api/v1/health
```

- `sudo systemctl reload tavletta-server` reads `tokens.toml` again
  (SIGHUP); it is also read again whenever it changes.
- `sudo systemctl restart tavletta-server` after changing
  `printers.toml` or installing fonts.
- Logs: `journalctl -u tavletta-server -f`.
- The time zone is the server's (`timedatectl`).

For a single user on a desktop there is the user service
`packaging/systemd/tavletta.service` instead.

### Updating { #native-update }

```sh
sudo /opt/tavletta/bin/pip install --upgrade "git+https://git.joshuahirsig.xyz/joshii/tavletta.git@v1.2.0"
sudo curl -fsSLo /etc/systemd/system/tavletta-server.service \
    https://git.joshuahirsig.xyz/joshii/tavletta/raw/tag/v1.2.0/packaging/systemd/tavletta-server.service
sudo systemctl daemon-reload
sudo systemctl restart tavletta-server
```

Back up `/etc/tavletta`, `/var/lib/tavletta/labels` and your fonts.

## Reverse proxy and HTTPS { #proxy }

The server speaks plain HTTP on `127.0.0.1`. Other computers reach it
through a reverse proxy that adds HTTPS. Give the server the proxy's
address with `--trusted-proxy`, so the log and the limits per client see
the real client, not the proxy.

### Traefik (Docker) { #traefik }

Traefik and Tavletta in one Docker network, Traefik with a fixed
address. In `compose.yaml`, remove `ports:` (the server is then reached
only through Traefik) and add:

```yaml
services:
  tavletta:
    # ... as in compose.yaml, without ports:
    networks: [proxy]
    command:
      - "--mcp"
      - "--printers"
      - "/config/printers.toml"
      - "--tokens"
      - "/config/tokens.toml"
      - "--trusted-proxy"
      - "172.30.0.10/32"          # Traefik's address in the network
    labels:
      - "traefik.enable=true"
      - "traefik.http.routers.tavletta.rule=Host(`labels.example.com`)"
      - "traefik.http.routers.tavletta.entrypoints=websecure"
      - "traefik.http.routers.tavletta.tls.certresolver=letsencrypt"
      - "traefik.http.services.tavletta.loadbalancer.server.port=8790"

networks:
  proxy:
    external: true                # Traefik's network, Traefik at 172.30.0.10
```

### nginx { #nginx }

The server on `127.0.0.1` answers only requests for a local host name
(against DNS rebinding): nginx must not pass on the public name. Its
default (`127.0.0.1:8790`) is right; do not add
`proxy_set_header Host $host`.

```nginx
server {
    listen 443 ssl;
    server_name labels.example.com;
    ssl_certificate     /etc/ssl/certs/labels.example.com.pem;
    ssl_certificate_key /etc/ssl/private/labels.example.com.key;

    client_max_body_size 4m;           # as the server's --max-body
    location / {
        proxy_pass http://127.0.0.1:8790;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_read_timeout 330s;       # print requests may wait up to 300 s
    }
}
```

Start the server with `--trusted-proxy 127.0.0.1/32` (append it to
`ExecStart` in the service, or to `command:` in `compose.yaml`).

### Caddy { #caddy }

Caddy passes on the public host name by default; set it to the server's
own:

```text
labels.example.com {
    reverse_proxy 127.0.0.1:8790 {
        header_up Host {upstream_hostport}
    }
}
```

Caddy gets a certificate by itself. Start the server with
`--trusted-proxy 127.0.0.1/32`.

### Firewall { #firewall }

- Open only the proxy's port 443, and only for the networks that need
  it (the inventory tool's server, the clients' VLAN).
- Port 8790 stays closed: the server listens on `127.0.0.1` (Docker
  publishes it on `127.0.0.1` only).
- Outgoing from the server to the printers: TCP 9100 and 631.
- Never make the server reachable from the internet.

## Fonts { #fonts }

A label is drawn with the fonts of the computer that prints it. A text
in a font the server does not have is drawn in another one; the preview
and the print job then warn with `font_missing` (for example
`Element 2: the font Source Sans 3 is not installed – another font is
used`).

- Docker: the image has DejaVu, Source Sans 3, Liberation and Carlito.
  Copy other fonts (`.ttf`, `.otf`, e.g. the corporate font) into
  `fonts/` and restart: `sudo docker compose restart`. More Debian font
  packages: build the image yourself, `sudo docker compose -f compose.yaml
  -f compose.build.yaml build --build-arg FONTS="fonts-dejavu-core
  fonts-adobe-sourcesans3 fonts-liberation fonts-crosextra-carlito
  fonts-noto-core"`, then `up -d` with the same two files.
- Native: install the distribution's font packages (Debian, Ubuntu:
  `fonts-adobe-sourcesans3`; Fedora: `adobe-source-sans-pro-fonts`) or
  copy the font files to `/usr/local/share/fonts/`, then
  `sudo fc-cache -f` and restart the service.
- Check what the server has: `fc-list : family` (in the container:
  `sudo docker compose exec tavletta fc-list : family`).
- Fonts may need a license for use on a server; Source Sans 3,
  Liberation, Carlito, DejaVu and Noto are free.

## Security checklist { #security }

- One token per program or device, with the smallest role
  (`print` for programs, `preview` for monitoring) and limited to its
  printers (`--printer`); an expiry date for temporary ones.
- Tokens kept in a password manager; `tokens.toml` mode 600.
- `printers.toml` in use: programs can print only on these printers.
- `max_copies` per printer set to what is sensible.
- The server reachable only through the HTTPS proxy, only from the
  networks that need it; never from the internet.
- `--trusted-proxy` set to the proxy's own address, nothing wider.
- Updates: watch the releases and update within weeks; the image
  of each release brings Debian's security updates of its day
  (`docker compose pull`).
- Backup of `config/` (or `/etc/tavletta`) and the labels.

The details of every protection (limits, rate limit, file access, web
pages): [docs/server.md](https://git.joshuahirsig.xyz/joshii/tavletta/src/branch/main/docs/server.md#security).

## Troubleshooting { #troubleshooting }

| Symptom | Cause and remedy |
|---|---|
| A printer is `UNREACHABLE` (`printer_timeout`, `printer_not_found`) | The server cannot reach it: test from the server with `nc -vz <printer> 9100` (631 for IPP); check the address, the firewall, that the printer is on. In Docker the container must reach the printer's network. |
| `401 unauthorized` | Token missing, mistyped, expired or removed from `tokens.toml`. The header is `Authorization: Bearer <token>`. |
| `429 too_many_requests` | Ten wrong tokens from this address within a minute: fix the token, wait a minute. |
| `429 queue_full` | 100 jobs (20 per token) are waiting: the printer is slow or stopped; check it, retry later. |
| `503 busy` | All request or render slots are taken for a moment: retry after a second or two. |
| `404 printer_not_found` | Not in `printers.toml`, or the token may not use it (`--printer`). |
| `413 label_too_large` | The label has too many elements, too long texts or renders larger than 40 megapixels: simplify it or use fewer data rows per request. |
| `421 wrong_host` | A reverse proxy passes the public host name to a server on `127.0.0.1`: see [nginx](#nginx) and [Caddy](#caddy). |
| `font_missing` in the warnings, text looks wrong | The font is not installed on the server: see [Fonts](#fonts). |
| `409 counter_not_saved` | The label file is not writable for the server's user: `chown` it to the user (Docker: 1000, native: `tavletta`). |
| Date or time on labels off by an hour or two | The container runs on UTC: set `TAVLETTA_TZ=Europe/Zurich` in `.env`, then `docker compose up -d`. |
| The server does not start | `docker compose logs` or `journalctl -u tavletta-server`: a mistake in `printers.toml` or `tokens.toml` is named with its key. |
| Permission denied on the folders (Fedora, RHEL with SELinux and Podman) | Add `:Z` to the volumes in `compose.yaml`. |
