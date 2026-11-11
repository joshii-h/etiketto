"""Translation helpers: the catalogue, plural-free lookups, RTL and documentation links."""

from __future__ import annotations

import gettext
import sys
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
_ = TRANSLATION.gettext


def pgettext(context: str, message: str) -> str:
    """A translation with context, for words with several meanings ("May")."""
    return TRANSLATION.pgettext(context, message)


RTL_LANGUAGES = {"ar", "he", "fa", "ur"}


def translation_is_rtl(translation=TRANSLATION) -> bool:
    """True if the loaded catalog is a right-to-left language. GTK only takes
    the direction from the system locale, not from LANGUAGE."""
    language = translation.info().get("language", "") if hasattr(translation, "info") else ""
    return language.split("_")[0].split("-")[0].lower() in RTL_LANGUAGES


def isolate(text: str) -> str:
    """Wrap left-to-right content (links, codes) so it stays intact in RTL text."""
    return "\u2068" + text + "\u2069"


DOCS_URL = "https://etiketto.joshuahirsig.xyz"
# languages the documentation is available in (site/po/<lang>.po)
DOCS_LANGUAGES = ("en", "de")


def docs_url(translation=TRANSLATION) -> str:
    """The documentation in the interface language, English otherwise."""
    language = translation.info().get("language", "") if hasattr(translation, "info") else ""
    short = language.split("_")[0].split("-")[0].lower()
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
