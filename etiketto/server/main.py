"""`etiketto serve` and `etiketto mcp`: settings from the command line."""

from __future__ import annotations

import logging
import os
import signal
import stat
import sys
from datetime import date, datetime
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
    if token is not None and not all(33 <= ord(c) < 127 for c in token):
        # an HTTP header carries it: anything else would never match
        raise CliError(
            _("The access token may only contain printable ASCII characters without spaces"),
            EXIT_USAGE,
        )
    if token is not None and len(token) < MIN_TOKEN:
        raise CliError(
            _("The access token must be at least %d characters long") % MIN_TOKEN, EXIT_USAGE
        )
    return token


def _logging() -> None:
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s"
    )


def _configured(args):
    """printers.toml and tokens.toml, checked: (printers or None, tokens or None)."""
    from etiketto.server.config import ConfigError, TokenStore, load_printers

    if args.printers and args.allow_printer:
        raise CliError(_("Use either --printers or --allow-printer"), EXIT_USAGE)
    if getattr(args, "tokens", None) and getattr(args, "token_file", None):
        raise CliError(_("Use either --tokens or --token-file"), EXIT_USAGE)
    try:
        printers = load_printers(args.printers) if args.printers else None
        tokens = TokenStore(args.tokens, printers) if getattr(args, "tokens", None) else None
    except ConfigError as e:
        raise CliError(str(e), EXIT_FILE) from None
    return printers, tokens


def _service(args, printers):
    from etiketto.server.service import Service

    folder = labels_dir(args.labels_dir)
    return Service(folder, tuple(args.allow_printer), printers=printers, discover=args.discover)


def serve(args) -> int:
    from etiketto.server.http import Config, Server, binds_loopback

    _logging()
    printers, tokens = _configured(args)
    token = read_token(args.token_file) if tokens is None else None
    if token is None and tokens is None and not binds_loopback(args.host):
        raise CliError(
            _(
                "Refusing to listen on %s without an access token: set ETIKETTO_TOKEN or "
                "use --token-file"
            )
            % args.host,
            EXIT_USAGE,
        )
    service = _service(args, printers)
    config = Config(args.host, args.port, token, args.mcp, tuple(args.allow_origin), tokens=tokens)
    try:
        server = Server(config, service)
    except OSError as e:
        service.close()
        raise CliError(
            _("Cannot listen on %(address)s: %(reason)s")
            % {"address": "%s:%d" % (args.host, args.port), "reason": e.strerror or e},
            EXIT_USAGE,
        ) from None
    if tokens is not None:  # revoked tokens: read the file again on SIGHUP too
        signal.signal(signal.SIGHUP, lambda _signum, _frame: tokens.reload())
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

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    printers, _tokens = _configured(args)
    return run_stdio(_service(args, printers))


# --- etiketto token new -------------------------------------------------------------


def default_tokens_file() -> Path:
    from gi.repository import GLib

    return Path(GLib.get_user_config_dir()) / "etiketto" / "tokens.toml"


def _expiry(text: str | None) -> str:
    """A date or date and time as TOML writes it; "" for none."""
    if not text:
        return ""
    try:
        if "T" in text or " " in text.strip():
            return datetime.fromisoformat(text.strip()).isoformat()
        return date.fromisoformat(text.strip()).isoformat()
    except ValueError:
        raise CliError(
            _("%s is not a date, expected e.g. 2026-12-31 or 2026-12-31T08:30") % text, EXIT_USAGE
        ) from None


def token_new(args) -> int:
    """Make a token, add its hash to tokens.toml, show the token once."""
    from etiketto.model import write_file
    from etiketto.server.config import NAME, hash_token, new_token, token_entry

    if not NAME.match(args.name):
        raise CliError(_("Not a valid name: letters, digits, . _ - (up to 64)"), EXIT_USAGE)
    path = args.tokens or default_tokens_file()
    printers = _printers_file(args.printers)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    token = new_token()
    entry = token_entry(
        args.name,
        hash_token(token, "scrypt" if args.scrypt else "sha256"),
        args.role,
        args.printer,
        _expiry(args.expires),
        args.description or "",
    )
    if old.strip() and any(e.caller.name == args.name for e in _entries(path, printers)):
        raise CliError(_("A token named %s exists already") % args.name, EXIT_USAGE)
    _check_new(path, old + entry, printers)
    if not path.exists():  # only for this user, from the start
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    write_file(path, (old + entry).encode())
    sys.stdout.write(token + "\n")
    sys.stdout.flush()
    sys.stderr.write(
        "etiketto: "
        + _("The token is shown only this once; %s keeps only its hash.") % path
        + "\n"
    )
    return EXIT_OK


def _printers_file(path: Path | None):
    from etiketto.server.config import ConfigError, load_printers

    try:
        return load_printers(path) if path else None
    except ConfigError as e:
        raise CliError(str(e), EXIT_FILE) from None


def _entries(path: Path, printers):
    from etiketto.server.config import ConfigError, load_tokens

    try:
        return load_tokens(path, printers)
    except ConfigError as e:
        raise CliError(str(e), EXIT_FILE) from None


def _check_new(path: Path, text: str, printers) -> None:
    """The file with the new entry must read as the server reads it."""
    import tempfile

    from etiketto.server.config import ConfigError, load_tokens

    with tempfile.TemporaryDirectory() as folder:
        trial = Path(folder) / path.name
        trial.write_text(text, encoding="utf-8")
        try:
            load_tokens(trial, printers)
        except ConfigError as e:
            raise CliError(str(e).replace(str(trial), str(path)), EXIT_FILE) from None
