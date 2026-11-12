"""The server's configuration files: printers.toml and tokens.toml.

printers.toml names the printers clients may use; with it, a request
chooses a printer by name and nothing else can be printed on:

    [printers.lager-cab]
    backend = "cab"                 # printer model (etiketto list-printers)
    host = "cab-lager.example.com"  # address or printer URI
    description = "Shipping labels"
    location = "Warehouse, gate 2"
    media = "100 x 50 mm"           # what is loaded, for people
    default = true                  # used when a request names no printer
    max_copies = 100
    cut = "full"                    # settings as in the app
    mode = "standard"
    safe_mm = 1.5
    data_format = "jpeg"
    color = { brightness = 0, red = 0, green = 0, blue = 0 }

tokens.toml names the access tokens, with a role, never the token itself:

    [tokens.shipping-terminal]
    hash = "sha256:9f86d0..."       # `etiketto token new` writes it
    role = "print"                  # preview, print or admin
    printers = ["lager-cab"]        # only these; all by default
    expires = 2027-12-31            # a date or a date and time
    description = "PC at gate 2"

Both are read strictly: an unknown key, a wrong type or an unknown printer
model stops the server with a message naming the file and the key.
Removing a token from tokens.toml revokes it: the file is read again when
it changes (and on SIGHUP).
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
import secrets
import stat
import threading
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from etiketto.backends import get_backend
from etiketto.backends.base import ColorAdjust
from etiketto.ui.i18n import _

log = logging.getLogger(__name__)

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
ROLES = ("preview", "print", "admin")
MAX_FILE = 1024 * 1024  # bytes of a configuration file
MAX_COPIES = 9999
TOKEN_PREFIX = "etk_"
# scrypt for tokens chosen by people; random tokens need no slow hash
SCRYPT = {"n": 2**14, "r": 8, "p": 1}


class ConfigError(Exception):
    """A configuration file that cannot be used; the message is translated."""


def _problem(path: Path, key: str, problem: str) -> ConfigError:
    return ConfigError(
        _("%(file)s: %(key)s: %(problem)s") % {"file": path, "key": key, "problem": problem}
    )


def read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as file:
            raw = file.read(MAX_FILE + 1)
    except OSError as e:
        raise ConfigError(
            _("Cannot read %(file)s: %(reason)s") % {"file": path, "reason": e.strerror or e}
        ) from None
    if len(raw) > MAX_FILE:
        raise ConfigError(
            _("Cannot read %(file)s: %(reason)s") % {"file": path, "reason": "> 1 MB"}
        )
    try:
        return tomllib.loads(raw.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(
            _("Cannot read %(file)s: %(reason)s") % {"file": path, "reason": e}
        ) from None


class _Table:
    """One table of a file, its keys taken out one by one and checked."""

    def __init__(self, path: Path, where: str, data: object):
        self.path, self.where = path, where
        if not isinstance(data, dict):
            raise _problem(path, where, _("expected a table"))
        self.data = dict(data)

    def fail(self, key: str, problem: str) -> ConfigError:
        return _problem(self.path, "%s.%s" % (self.where, key), problem)

    def text(self, key: str, required: bool = False, default: str = "") -> str:
        value = self.data.pop(key, None)
        if value is None:
            if required:
                raise self.fail(key, _("missing"))
            return default
        if not isinstance(value, str) or len(value) > 1000:
            raise self.fail(key, _("expected text"))
        if any(ord(c) < 32 or ord(c) == 127 for c in value):  # it reaches logs and answers
            raise self.fail(key, _("control characters are not allowed"))
        return value

    def flag(self, key: str) -> bool:
        value = self.data.pop(key, False)
        if not isinstance(value, bool):
            raise self.fail(key, _("expected true or false"))
        return value

    def number(self, key: str, lower: float, upper: float, whole: bool = False) -> Any:
        value = self.data.pop(key, None)
        kinds = (int,) if whole else (int, float)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, kinds) or not lower <= value <= upper:
            raise self.fail(
                key,
                _("expected a number from %(min)s to %(max)s") % {"min": lower, "max": upper},
            )
        return value

    def choice(self, key: str, allowed: tuple[str, ...]) -> str | None:
        value = self.data.pop(key, None)
        if value is not None and value not in allowed:
            raise self.fail(key, _("expected one of: %s") % ", ".join(allowed))
        return value

    def done(self) -> None:
        if self.data:
            raise self.fail(sorted(self.data)[0], _("unknown key"))


def _tables(path: Path, data: dict, section: str) -> dict[str, object]:
    """The named tables of [section.<name>]; the names checked."""
    if extra := sorted(set(data) - {section}):
        raise _problem(path, extra[0], _("unknown key"))
    entries = data.get(section)
    if not isinstance(entries, dict) or not entries:
        raise _problem(path, section, _("nothing defined"))
    for name in entries:
        if not NAME.match(name):
            raise _problem(
                path,
                "%s.%s" % (section, name),
                _("not a valid name: letters, digits, . _ - (up to 64)"),
            )
    return entries


# --- printers -------------------------------------------------------------------


@dataclass(frozen=True)
class NamedPrinter:
    name: str
    backend: str  # backend id
    host: str
    description: str = ""
    location: str = ""
    media: str = ""
    default: bool = False
    max_copies: int = MAX_COPIES
    settings: Mapping[str, Any] = field(default_factory=dict)  # as in settings.json

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "printer": self.backend,
            "host": self.host,
            "description": self.description or None,
            "location": self.location or None,
            "media": self.media or None,
            "default": self.default,
            "max_copies": self.max_copies,
        }


def load_printers(path: Path) -> dict[str, NamedPrinter]:
    from etiketto.server.service import host_ok

    entries = _tables(path, read_toml(path), "printers")
    printers = {name: _printer(path, name, data, host_ok) for name, data in entries.items()}
    defaults = [p.name for p in printers.values() if p.default]
    if len(defaults) > 1:
        raise _problem(
            path, "printers.%s.default" % defaults[1], _("only one printer can be the default")
        )
    return printers


def _printer(path: Path, name: str, data: object, host_ok) -> NamedPrinter:
    table = _Table(path, "printers." + name, data)
    backend_id = table.text("backend", required=True)
    try:
        backend = get_backend(backend_id)
    except KeyError:
        raise table.fail("backend", _("Unknown printer model: %s") % backend_id) from None
    host = table.text("host", required=True)
    if not host_ok(host):
        raise table.fail("host", _("not a valid printer address"))
    settings = _settings(table, backend.capabilities, host)
    printer = NamedPrinter(
        name,
        backend_id,
        host,
        table.text("description"),
        table.text("location"),
        table.text("media"),
        table.flag("default"),
        table.number("max_copies", 1, MAX_COPIES, whole=True) or MAX_COPIES,
        settings,
    )
    table.done()
    return printer


def _settings(table: _Table, caps, host: str) -> dict[str, Any]:
    """The printer's settings as settings.json keeps them for a model."""
    if not caps.cut_modes and "cut" in table.data:
        raise table.fail("cut", _("this printer model does not cut"))
    if not caps.supports_color_adjust and "color" in table.data:
        raise table.fail("color", _("this printer model has no colour settings"))
    found = {
        "cut": table.choice("cut", caps.cut_modes),
        "mode": table.choice("mode", tuple(m.id for m in caps.modes)),
        "safe_mm": table.number("safe_mm", 0, 10),
        "data_format": table.choice("data_format", caps.data_formats),
    }
    if "color" in table.data:
        found["color_adjust"] = _color(table, table.data.pop("color"))
    return {"host": host} | {key: value for key, value in found.items() if value is not None}


def _color(parent: _Table, data: object) -> dict[str, int]:
    table = _Table(parent.path, parent.where + ".color", data)
    steps = ColorAdjust.STEPS
    values = {key: table.number(key, -steps, steps, whole=True) or 0 for key in ColorAdjust.FIELDS}
    table.done()
    return values


# --- tokens ------------------------------------------------------------------------


@dataclass(frozen=True)
class Caller:
    """Who sends a request: the token's name, its role and the printers it
    may use (None: all)."""

    name: str
    role: str = "admin"
    printers: frozenset[str] | None = None

    def may(self, role: str) -> bool:
        return ROLES.index(self.role) >= ROLES.index(role)

    def may_use(self, printer: str) -> bool:
        return self.printers is None or printer in self.printers


LOCAL = Caller("local")  # no token: the server listens on loopback only


@dataclass(frozen=True)
class TokenEntry:
    caller: Caller
    hash: str
    expires: dt.datetime | None = None
    id: str = ""  # the id in the token (etk_<id>_<secret>): found without trying all

    def valid(self, now: dt.datetime) -> bool:
        return self.expires is None or now < self.expires


# what scrypt may take: 128 * n * r * p bytes of memory
SCRYPT_MAXMEM = 2**28
TOKEN_ID = re.compile(r"etk_([0-9a-f]{12})_[A-Za-z0-9_-]{16,}\Z")


def hash_token(token: str, method: str = "sha256") -> str:
    """The form tokens.toml keeps: "sha256:<hex>" or
    "scrypt:<n>:<r>:<p>:<salt>:<hash>" (base64)."""
    if method == "sha256":
        return "sha256:" + hashlib.sha256(token.encode()).hexdigest()
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(token.encode(), salt=salt, dklen=32, maxmem=SCRYPT_MAXMEM, **SCRYPT)
    return "scrypt:%(n)d:%(r)d:%(p)d:" % SCRYPT + ":".join(
        base64.b64encode(part).decode() for part in (salt, digest)
    )


def _parse_hash(value: str) -> tuple:
    """() if `value` is no hash this module makes."""
    method, _sep, rest = value.partition(":")
    if method == "sha256" and re.fullmatch(r"[0-9a-f]{64}", rest):
        return ("sha256", bytes.fromhex(rest))
    parts = rest.split(":")
    return _scrypt(parts) if method == "scrypt" and len(parts) == 5 else ()


def _scrypt(parts: list[str]) -> tuple:
    """scrypt parameters a check can run with: digits only, n a power of
    two, within the memory scrypt may take; salt and hash not too short."""
    if not all(x.isascii() and x.isdigit() for x in parts[:3]):
        return ()
    n, r, p = (int(x) for x in parts[:3])
    try:
        salt, digest = (base64.b64decode(x, validate=True) for x in parts[3:])
    except (ValueError, binascii.Error):
        return ()
    sane = _scrypt_cost_ok(n, r, p) and len(salt) >= 8 and 16 <= len(digest) <= 64
    return ("scrypt", n, r, p, salt, digest) if sane else ()


def _scrypt_cost_ok(n: int, r: int, p: int) -> bool:
    if not (2 <= n <= 2**20 and n & (n - 1) == 0):
        return False
    return 1 <= r <= 32 and 1 <= p <= 16 and 128 * n * r * p < SCRYPT_MAXMEM


_SCRYPTS = threading.BoundedSemaphore(2)  # slow checks at once: a CPU and memory each


def token_matches(token: str, stored: str) -> bool:
    parsed = _parse_hash(stored)
    if not parsed:
        return False
    if parsed[0] == "sha256":
        return hmac.compare_digest(hashlib.sha256(token.encode()).digest(), parsed[1])
    _method, n, r, p, salt, digest = parsed
    with _SCRYPTS:
        try:
            mine = hashlib.scrypt(
                token.encode(), salt=salt, n=n, r=r, p=p, dklen=len(digest), maxmem=SCRYPT_MAXMEM
            )
        except ValueError:  # parameters the library refuses after all
            return False
    return hmac.compare_digest(mine, digest)


def _expiry(table: _Table) -> dt.datetime | None:
    """A date and time, or a date: valid through that whole day."""
    value = table.data.pop("expires", None)
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.astimezone()  # local time if none given
    if isinstance(value, dt.date):
        return dt.datetime.combine(value + dt.timedelta(days=1), dt.time()).astimezone()
    raise table.fail("expires", _("expected a date such as 2027-12-31"))


def load_tokens(path: Path, printers: Mapping[str, NamedPrinter] | None) -> list[TokenEntry]:
    _check_mode(path)
    entries = _tables(path, read_toml(path), "tokens")
    tokens = [_token(path, name, data, printers) for name, data in entries.items()]
    seen: set[str] = set()
    for token in tokens:
        # one token under two names (and maybe two roles): which one counts?
        keys = {token.hash} if token.hash.startswith("sha256:") else set()
        keys |= {"id:" + token.id} if token.id else set()
        if keys & seen:
            raise _problem(path, "tokens.%s.hash" % token.caller.name, _("the same token twice"))
        seen |= keys
    return tokens


def _check_mode(path: Path) -> None:
    """Anyone who can change tokens.toml can give themselves a token."""
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return  # read_toml says what is wrong
    if mode & stat.S_IWOTH:
        raise ConfigError(
            _("%s can be changed by every user of this computer: chmod 600 it") % path
        )
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        log.warning("%s can be read or changed by others than its owner: chmod 600 it", path)


def _token(path: Path, name: str, data: object, printers) -> TokenEntry:
    table = _Table(path, "tokens." + name, data)
    stored = table.text("hash", required=True)
    if not _parse_hash(stored):
        raise table.fail("hash", _("not a token hash: expected sha256:… or scrypt:…"))
    role = table.choice("role", ROLES)
    if role is None:
        raise table.fail("role", _("missing"))
    ident = table.text("id")
    if ident and not re.fullmatch(r"[0-9a-f]{12}", ident):
        raise table.fail("id", _("expected text"))
    chosen = _token_printers(table, printers)
    expires = _expiry(table)
    table.text("description")
    table.done()
    return TokenEntry(Caller(name, role, chosen), stored, expires, ident)


def _token_printers(table: _Table, printers) -> frozenset[str] | None:
    allowed = table.data.pop("printers", None)
    if allowed is None:
        return None
    if not isinstance(allowed, list) or not all(isinstance(p, str) for p in allowed):
        raise table.fail("printers", _("expected a list of printer names"))
    if printers is None:
        raise table.fail("printers", _("printers can only be limited with --printers"))
    if unknown := [p for p in allowed if p not in printers]:
        raise table.fail("printers", _("Unknown printer: %s") % unknown[0])
    return frozenset(allowed)


class TokenStore:
    """The tokens of tokens.toml, read again when the file changes (or
    after SIGHUP: request_reload()). A file that became unusable lets no
    token in until it is fixed: a revoked token must not stay valid
    because of a typo elsewhere."""

    def __init__(self, path: Path, printers: Mapping[str, NamedPrinter] | None = None):
        self.path = path
        self.printers = printers
        self._lock = threading.Lock()
        self._entries = load_tokens(path, printers)  # ConfigError at startup
        self._stamp = self._stat()
        # tokens checked before, by their SHA-256: no slow hash again
        self._known: dict[bytes, TokenEntry] = {}
        self._reload = threading.Event()

    def _stat(self) -> tuple[int, ...] | None:
        """What changes when the file is edited or replaced."""
        try:
            info = os.stat(self.path)
        except OSError:
            return None
        return (info.st_ino, info.st_mtime_ns, info.st_ctime_ns, info.st_size)

    def request_reload(self) -> None:
        """For a signal handler: only sets a flag, the next request reads."""
        self._reload.set()

    def reload(self) -> None:
        self._reload.clear()
        with self._lock:
            self._stamp = self._stat()
            try:
                entries = load_tokens(self.path, self.printers)
            except ConfigError as e:
                self._entries, self._known = [], {}
                log.error("no token is valid until %s is fixed: %s", self.path, e)
                return
            # tokens known before stay known if their entry is unchanged
            self._known = {k: e for k, e in self._known.items() if e in entries}
            self._entries = entries
            log.info("read %s: %d tokens", self.path, len(entries))

    def find(
        self, token: str, now: dt.datetime | None = None, cheap_only: bool = False
    ) -> Caller | None:
        """The caller of a token. `cheap_only`: no scrypt check of a token
        not seen before (for an address that keeps sending wrong ones)."""
        if self._reload.is_set() or self._stat() != self._stamp:
            self.reload()
        now = now or dt.datetime.now().astimezone()
        key = hashlib.sha256(token.encode()).digest()
        with self._lock:
            entries = [e for e in self._entries if e.valid(now)]
            known = self._known.get(key)
        if known is not None:
            return known.caller if known in entries else None
        entry = self._match(token, entries, cheap_only)
        if entry is not None:
            with self._lock:
                self._known[key] = entry
            return entry.caller
        return None

    @staticmethod
    def _match(token: str, entries: list[TokenEntry], cheap_only: bool) -> TokenEntry | None:
        """The entry of a token: by the id in it, else SHA-256 entries (fast),
        else scrypt entries one after the other."""
        ident = TOKEN_ID.match(token)
        if ident:
            entries = [e for e in entries if e.id == ident.group(1)] or entries
        order = sorted(entries, key=lambda e: not e.hash.startswith("sha256:"))
        if cheap_only:
            order = [e for e in order if e.hash.startswith("sha256:")]
        return next((e for e in order if token_matches(token, e.hash)), None)


def new_token() -> tuple[str, str]:
    """A random token and its id: etk_<id>_<secret>."""
    ident = secrets.token_hex(6)
    return "%s%s_%s" % (TOKEN_PREFIX, ident, secrets.token_urlsafe(32)), ident


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % "".join(
        c if ord(c) >= 32 and c != "\x7f" else "\\u%04x" % ord(c) for c in escaped
    )


def token_entry(
    name: str,
    hash_: str,
    role: str,
    printers: list[str],
    expires: str,
    description: str,
    ident: str = "",
) -> str:
    """The lines of a new entry in tokens.toml; the name quoted, so a dot
    in it does not make a table inside a table."""
    lines = [
        "",
        "[tokens.%s]" % _toml_string(name),
        "role = %s" % _toml_string(role),
        "hash = %s" % _toml_string(hash_),
    ]
    if ident:
        lines.append("id = %s" % _toml_string(ident))
    if printers:
        lines.append("printers = [%s]" % ", ".join(_toml_string(p) for p in printers))
    if expires:
        lines.append("expires = %s" % expires)
    if description:
        lines.append("description = %s" % _toml_string(description))
    return "\n".join(lines) + "\n"
