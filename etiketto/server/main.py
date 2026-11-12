"""`etiketto serve` and `etiketto mcp`: settings from the command line."""

from __future__ import annotations

import logging
import os
import signal
import stat
import sys
from pathlib import Path

from etiketto.cli import EXIT_FILE, EXIT_OK, EXIT_USAGE, CliError
from etiketto.ui.i18n import _

TOKEN_VARIABLE = "ETIKETTO_TOKEN"
MIN_TOKEN = 16  # characters; `openssl rand -hex 32` gives 64

log = logging.getLogger("etiketto.server")


def default_labels_dir() -> Path:
    from gi.repository import GLib

    return Path(GLib.get_user_data_dir()) / "etiketto" / "labels"


def labels_dir(given: Path | None) -> Path:
    """The folder given, or the default one (made if it is not there yet)."""
    if given is None:
        folder = default_labels_dir()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        return folder
    if not given.is_dir():
        raise CliError(_("The labels folder %s does not exist") % given, EXIT_FILE)
    return given


def read_token(path: Path | None, environ=os.environ) -> str | None:
    """The access token from --token-file, else from ETIKETTO_TOKEN."""
    if path is None:
        token = environ.get(TOKEN_VARIABLE, "").strip() or None
    else:
        try:
            token = path.read_text(encoding="utf-8").strip()
            mode = path.stat().st_mode
        except (OSError, ValueError):
            raise CliError(_("Cannot read the token file %s") % path, EXIT_FILE) from None
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            log.warning("the token file %s can be read by others: chmod 600 it", path)
    if token is not None and len(token) < MIN_TOKEN:
        raise CliError(
            _("The access token must be at least %d characters long") % MIN_TOKEN, EXIT_USAGE
        )
    return token


def _logging() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s"
    )


def serve(args) -> int:
    from etiketto.server.http import Config, Server, binds_loopback
    from etiketto.server.service import Service

    _logging()
    token = read_token(args.token_file)
    if token is None and not binds_loopback(args.host):
        raise CliError(
            _(
                "Refusing to listen on %s without an access token: set ETIKETTO_TOKEN or "
                "use --token-file"
            )
            % args.host,
            EXIT_USAGE,
        )
    service = Service(labels_dir(args.labels_dir), tuple(args.allow_printer))
    config = Config(args.host, args.port, token, args.mcp, tuple(args.allow_origin))
    try:
        server = Server(config, service)
    except OSError as e:
        service.close()
        raise CliError(
            _("Cannot listen on %(address)s: %(reason)s")
            % {"address": "%s:%d" % (args.host, args.port), "reason": e.strerror or e},
            EXIT_USAGE,
        ) from None
    return _run(server, service)


def _run(server, service) -> int:
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    host, port = server.server_address[:2]
    log.info(
        "listening on http://%s:%d%s (labels: %s)",
        host,
        port,
        " with MCP at /mcp" if server.mcp else "",
        service.labels.root,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping: jobs printing finish their label")
    finally:
        server.server_close()
        service.close()
    return EXIT_OK


def mcp(args) -> int:
    from etiketto.server.mcp import run_stdio
    from etiketto.server.service import Service

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    return run_stdio(Service(labels_dir(args.labels_dir), tuple(args.allow_printer)))
