"""Translation helpers: the catalogue, plural-free lookups, RTL and documentation links."""

from __future__ import annotations

import contextlib
import gettext
import sys
from collections.abc import Iterator
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path


def _locale_dir() -> Path:
    """Translations: <prefix>/share/locale (pip, install.sh, Flatpak) or a checkout's etiketto/locale."""
    for candidate in (
        Path(__file__).parent.parent / "locale",
        Path(sys.prefix) / "share" / "locale",
        Path("/app/share/locale"),
    ):
        if any(candidate.glob("*/LC_MESSAGES/etiketto.mo")):
            return candidate
    return Path(__file__).parent.parent / "locale"


LOCALE_DIR = _locale_dir()
TRANSLATION = gettext.translation("etiketto", LOCALE_DIR, fallback=True)
# the server answers each request in the language it asks for (see
# language()); everywhere else this stays None: the system language
_ACTIVE: ContextVar[gettext.NullTranslations | None] = ContextVar("translation", default=None)


def _catalog():
    return _ACTIVE.get() or TRANSLATION


def _(message: str) -> str:
    return _catalog().gettext(message)


def pgettext(context: str, message: str) -> str:
    """A translation with context, for words with several meanings ("May")."""
    return _catalog().pgettext(context, message)


def _quality(params: str) -> float:
    """The weight of a language range: "q=0.8" -> 0.8, none -> 1."""
    params = params.strip()
    if not params.startswith("q="):
        return 1.0
    try:
        return float(params[2:])
    except ValueError:
        return 0.0


def accepted_languages(header: str) -> list[str]:
    """The languages of an HTTP Accept-Language header as gettext names
    ("de-CH;q=0.9" -> "de_CH"), the preferred first. English is the
    language of the texts themselves: the list ends there, so "en, de"
    gives English and not the German catalog that exists."""
    ranked = []
    for position, part in enumerate(header.split(",")[:20]):
        tag, _sep, params = part.strip().partition(";")
        tag, quality = tag.strip(), _quality(params)
        if tag and tag != "*" and quality > 0 and tag.replace("-", "").isalnum():
            ranked.append((-quality, position, tag.split("-")))
    languages = []
    for *_order, parts in sorted(ranked):
        if parts[0].lower() == "en":
            break
        region = [p.upper() for p in parts[1:] if len(p) == 2][:1]
        languages.append("_".join([parts[0].lower()] + region))
    return languages


@lru_cache(maxsize=64)
def translation_for(languages: tuple[str, ...]) -> gettext.NullTranslations:
    """The catalog for the first of these languages there is one for."""
    if not languages:
        return gettext.NullTranslations()
    return gettext.translation("etiketto", LOCALE_DIR, languages=list(languages), fallback=True)


@contextlib.contextmanager
def language(header: str | None) -> Iterator[None]:
    """Translate into the languages of an Accept-Language header while
    the block runs, in this thread only; None: the system language."""
    if header is None:
        yield
        return
    token = _ACTIVE.set(translation_for(tuple(accepted_languages(header))))
    try:
        yield
    finally:
        _ACTIVE.reset(token)


RTL_LANGUAGES = {"ar", "he", "fa", "ur"}


def _language(translation) -> str:
    """The catalog's language without region: "pt" for "pt_BR"; "" if unknown."""
    language = translation.info().get("language", "") if hasattr(translation, "info") else ""
    return language.split("_")[0].split("-")[0].lower()


def translation_is_rtl(translation=TRANSLATION) -> bool:
    """True if the loaded catalog is a right-to-left language. GTK only takes
    the direction from the system locale, not from LANGUAGE."""
    return _language(translation) in RTL_LANGUAGES


def isolate(text: str) -> str:
    """Wrap left-to-right content (links, codes) so it stays intact in RTL text."""
    return "\u2068" + text + "\u2069"


DOCS_URL = "https://etiketto.joshuahirsig.xyz"
# languages the documentation is available in (site/po/<lang>.po)
DOCS_LANGUAGES = ("en", "de")


def docs_url(translation=TRANSLATION) -> str:
    """The documentation in the interface language, English otherwise."""
    short = _language(translation)
    return "%s/%s/" % (DOCS_URL, short if short in DOCS_LANGUAGES else "en")


def month_year(month: int, year: int) -> str:
    # translators: a month and a year, e.g. "May 2018"; reorder as your language needs
    return _("%(month)s %(year)d") % {"month": month_name(month), "year": year}


def month_name(month: int) -> str:
    return (
        pgettext("month name", "January"),
        pgettext("month name", "February"),
        pgettext("month name", "March"),
        pgettext("month name", "April"),
        pgettext("month name", "May"),
        pgettext("month name", "June"),
        pgettext("month name", "July"),
        pgettext("month name", "August"),
        pgettext("month name", "September"),
        pgettext("month name", "October"),
        pgettext("month name", "November"),
        pgettext("month name", "December"),
    )[month - 1]
