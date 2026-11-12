"""`tavletta serve` and `tavletta mcp`: settings from the command line."""

from __future__ import annotations

import logging
import os
import signal
import stat
import sys
from datetime import date, datetime
from pathlib import Path

from tavletta.cli import EXIT_FILE, EXIT_OK, EXIT_USAGE, CliError
from tavletta.ui.i18n import _

TOKEN_VARIABLE = "TAVLETTA_TOKEN"
MIN_TOKEN = 16  # characters; `openssl rand -hex 32` gives 64

log = logging.getLogger("tavletta.server")


def default_labels_dir() -> Path:
    from gi.repository import GLib

    return Path(GLib.get_user_data_dir()) / "tavletta" / "labels"


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
    """The access token from --token-file, else from TAVLETTA_TOKEN."""
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
    from tavletta.server.config import ConfigError, TokenStore, load_printers

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
    from tavletta.server.service import Service, pattern_problem

    for pattern in args.allow_printer:
        if pattern_problem(pattern.strip()):
            raise CliError(_("Not a valid printer address pattern: %s") % pattern, EXIT_USAGE)
    folder = labels_dir(args.labels_dir)
    return Service(
        folder,
        tuple(args.allow_printer),
        printers=printers,
        discover=args.discover,
        max_labels=args.max_labels,
        allow_pdf_pages=args.allow_pdf_pages,
    )


def serve(args) -> int:
    from tavletta.server.http import Config, Server, binds_loopback

    _logging()
    # first of all: a reload sent while the files are read must not end it
    loaded: list = []
    signal.signal(signal.SIGHUP, lambda _signum, _frame: _reload(loaded[0] if loaded else None))
    printers, tokens = _configured(args)
    loaded.append(tokens)
    _warn(args, printers, tokens)
    token = read_token(args.token_file) if tokens is None else None
    if token is None and tokens is None and not binds_loopback(args.host):
        raise CliError(
            _(
                "Refusing to listen on %s without an access token: set TAVLETTA_TOKEN or "
                "use --token-file"
            )
            % args.host,
            EXIT_USAGE,
        )
    _warm_up()
    service = _service(args, printers)
    config = Config(
        args.host,
        args.port,
        token,
        args.mcp,
        tuple(args.allow_origin),
        max_body=args.max_body * 1024 * 1024,
        tokens=tokens,
        trusted_proxies=tuple(args.trusted_proxy),
    )
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


def _warn(args, printers, tokens) -> None:
    """Settings that work, but likely not as meant."""
    for network in getattr(args, "trusted_proxy", []):
        if network.prefixlen == 0:
            log.warning(
                "--trusted-proxy %s trusts every address: any client can name any "
                "address in X-Forwarded-For; give the proxy's own address",
                network,
            )
    if tokens is None:
        return
    if os.environ.get(TOKEN_VARIABLE):
        log.warning("%s is ignored: the tokens of %s are used", TOKEN_VARIABLE, args.tokens)
    if printers is None and not args.allow_printer:
        log.warning(
            "--tokens without --printers: tokens with the role print can print on any "
            "address (limit them with --printers or --allow-printer)"
        )


def _reload(tokens) -> None:
    """SIGHUP (systemctl reload): tokens.toml is read again with the next
    request. Only a flag here: a signal handler must not take locks. Without
    tokens.toml there is nothing to read, but the server goes on running."""
    if tokens is not None:
        tokens.request_reload()


def _run(server, service) -> int:
    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    host, port = server.server_address[:2]
    log.info(
        "listening on http://%s:%d%s (labels: %s)",
        "[%s]" % host if ":" in host else host,
        port,
        " with MCP at /mcp" if server.mcp else "",
        service.labels.root,
    )
    notify("READY=1")  # systemd (Type=notify): started, a reload may come now
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("stopping: jobs printing finish their label")
    finally:
        notify("STOPPING=1")
        server.server_close()
        service.close()
    return EXIT_OK


_notify_socket: list[str] = []  # $NOTIFY_SOCKET, kept here


def notify(state: str) -> None:
    """Tell systemd (Type=notify) how the service is, if it asks: a datagram
    to $NOTIFY_SOCKET, as sd_notify() sends it, without a library."""
    import socket

    # taken out of the environment the first time: a program this one
    # starts (CUPS filters do not, but a plugin might) must not speak for it
    if "NOTIFY_SOCKET" in os.environ:
        _notify_socket[:] = [os.environ.pop("NOTIFY_SOCKET")]
    path = _notify_socket[0] if _notify_socket else ""
    if not path:
        return
    address = "\0" + path[1:] if path.startswith("@") else path  # an abstract socket
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.connect(address)
            sock.sendall(state.encode())
    except OSError as e:
        log.warning("cannot tell systemd %s: %s", state, e)


def mcp(args) -> int:
    from tavletta.server.mcp import run_stdio

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    printers, _tokens = _configured(args)
    _warm_up()
    return run_stdio(_service(args, printers))


def _warm_up() -> None:
    """Pango set up in the main thread, before requests and print jobs
    render in threads of their own (render.warm_up())."""
    from tavletta.render import warm_up

    warm_up()


# --- tavletta token new -------------------------------------------------------------


def default_tokens_file() -> Path:
    from gi.repository import GLib

    return Path(GLib.get_user_config_dir()) / "tavletta" / "tokens.toml"


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
    """Make a token (or take one from standard input), add its hash to
    tokens.toml, show the token once. The file is locked meanwhile: two
    at once do not lose one."""
    from tavletta.model import LockTimeout, locked
    from tavletta.server.config import NAME, new_token

    if not NAME.match(args.name):
        raise CliError(_("Not a valid name: letters, digits, . _ - (up to 64)"), EXIT_USAGE)
    path = args.tokens or default_tokens_file()
    printers = _printers_file(args.printers)
    token, ident = _chosen_token() if args.from_stdin else new_token()
    created = _create(path)
    try:
        try:
            with locked(path):
                _add(args, path, printers, token, ident)
        except LockTimeout:
            raise CliError(
                _("%s is in use by another program – try again later") % path, EXIT_FILE
            ) from None
    except BaseException:
        if created and path.exists() and path.stat().st_size == 0:
            path.unlink()  # the empty file made for it
        raise
    if args.from_stdin:
        message = _("The token %(name)s was added to %(file)s.") % {
            "name": args.name,
            "file": path,
        }
    else:
        sys.stdout.write(token + "\n")
        sys.stdout.flush()
        message = _("The token is shown only this once; %s keeps only its hash.") % path
    sys.stderr.write("tavletta: " + message + "\n")
    return EXIT_OK


def _chosen_token() -> tuple[str, str]:
    """A token people chose, from standard input (asked for without echo
    on a terminal): kept as an scrypt hash."""
    if sys.stdin.isatty():
        import getpass

        token = getpass.getpass(_("Access token: ")).strip()
    else:
        token = sys.stdin.readline().strip()
    if not token or not all(33 <= ord(c) < 127 for c in token):
        raise CliError(
            _("The access token may only contain printable ASCII characters without spaces"),
            EXIT_USAGE,
        )
    if len(token) < MIN_TOKEN:
        raise CliError(
            _("The access token must be at least %d characters long") % MIN_TOKEN, EXIT_USAGE
        )
    from tavletta.server.config import TOKEN_ID

    if TOKEN_ID.match(token):  # it would only be looked for by its id, which it has none of
        raise CliError(_("A chosen token must not look like a made one (tav_…)"), EXIT_USAGE)
    return token, ""


def _create(path: Path) -> bool:
    """Make tokens.toml for this user only, if it is not there yet."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return False
    except OSError as e:
        raise _unreadable(path, e) from None
    return True


def _unreadable(path: Path, error: Exception) -> CliError:
    reason = getattr(error, "strerror", None) or error
    return CliError(
        _("Cannot read %(file)s: %(reason)s") % {"file": path, "reason": reason}, EXIT_FILE
    )


def _add(args, path: Path, printers, token: str, ident: str) -> None:
    from tavletta.model import write_file
    from tavletta.server.config import hash_token, token_entry

    try:
        old = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise _unreadable(path, e) from None
    method = "scrypt" if args.scrypt or args.from_stdin else "sha256"
    entry = token_entry(
        args.name,
        hash_token(token, method),
        args.role,
        args.printer,
        _expiry(args.expires),
        args.description or "",
        ident,
    )
    _check_file_mode(path)
    if args.printer and printers is None:  # the new entry's names must be checked
        from tavletta.server.config import _problem

        problem = _("printers can only be limited with --printers")
        key = "tokens.%s.printers" % args.name
        raise CliError(str(_problem(path, key, problem)), EXIT_FILE)
    entries = _entries(path, printers) if old.strip() else []
    if any(e.caller.name == args.name for e in entries):
        raise CliError(_("A token named %s exists already") % args.name, EXIT_USAGE)
    _refuse_same_token(token, entries)
    _check_new(path, old + entry, printers)
    try:
        write_file(path, (old + entry).encode())
    except OSError as e:
        raise _unreadable(path, e) from None


def _check_file_mode(path: Path) -> None:
    """As the server reads it: a file every user may change is refused."""
    from tavletta.server.config import ConfigError, _check_mode

    try:
        _check_mode(path)
    except ConfigError as e:
        raise CliError(str(e), EXIT_FILE) from None


def _refuse_same_token(token: str, entries) -> None:
    """A token already in the file under another name (a chosen one could
    be): the same token twice, with maybe two roles."""
    from tavletta.server.config import token_matches

    for entry in entries:
        if token_matches(token, entry.hash):
            raise CliError(
                _("This token is in the file already, as %s") % entry.caller.name, EXIT_USAGE
            )


def _printers_file(path: Path | None):
    from tavletta.server.config import ConfigError, load_printers

    try:
        return load_printers(path) if path else None
    except ConfigError as e:
        raise CliError(str(e), EXIT_FILE) from None


def _entries(path: Path, printers):
    """The entries of the file; without printers.toml the printers they
    name are not checked (only the new entry's would be: it names none)."""
    from tavletta.server.config import ConfigError, load_tokens

    try:
        return load_tokens(path, printers, unchecked=printers is None)
    except ConfigError as e:
        raise CliError(str(e), EXIT_FILE) from None


def _check_new(path: Path, text: str, printers) -> None:
    """The file with the new entry must read as the server reads it."""
    import tempfile

    from tavletta.server.config import ConfigError, load_tokens

    with tempfile.TemporaryDirectory() as folder:
        trial = Path(folder) / path.name
        trial.write_text(text, encoding="utf-8")
        trial.chmod(0o600)
        try:
            load_tokens(trial, printers, unchecked=printers is None)
        except ConfigError as e:
            raise CliError(str(e).replace(str(trial), str(path)), EXIT_FILE) from None
