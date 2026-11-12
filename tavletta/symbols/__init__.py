"""Symbols: the icons of Material Design Icons (mdi.json.gz, made by
tools/mdi_symbols.py; Apache-2.0, see LICENSE.mdi), each one path in a
24 × 24 square. Names, aliases and categories are English, as in the icon
set, and so is the search.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from functools import cache, lru_cache
from pathlib import Path

from tavletta.symbols.path import PathError, Segment, parse

__all__ = ["PathError", "Symbol", "categories", "find", "remember", "search", "segments"]

DATA = Path(__file__).parent / "mdi.json.gz"
VIEW_BOX = 24.0  # every icon is drawn in a square of this size
MAX_RECENT = 24
_WORD = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True)
class Symbol:
    name: str  # "printer-outline": what a label file stores
    path: str  # SVG path data in the 24 × 24 square
    categories: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    # words for the search: of the name, the aliases, the categories
    words: tuple[frozenset[str], frozenset[str], frozenset[str]] = field(
        default=(frozenset(), frozenset(), frozenset()), compare=False, repr=False
    )


def _words(*texts: str) -> frozenset[str]:
    return frozenset(word for text in texts for word in _WORD.findall(text.lower()))


@cache
def _load() -> tuple[str, dict[str, Symbol], tuple[str, ...]]:
    with gzip.open(DATA, "rt", encoding="utf-8") as src:
        data = json.load(src)
    names = tuple(data["categories"])
    icons = {}
    for name, path, category_index, aliases in data["icons"]:
        cats = tuple(names[n] for n in category_index)
        icons[name] = Symbol(
            name, path, cats, tuple(aliases), (_words(name), _words(*aliases), _words(*cats))
        )
    return "%s %s" % (data["set"], data["version"]), icons, names


def source() -> str:
    """The icon set and its version: "Material Design Icons 7.4.47"."""
    return _load()[0]


def all_symbols() -> list[Symbol]:
    """Every symbol, sorted by name."""
    return list(_load()[1].values())


def find(name: str) -> Symbol | None:
    return _load()[1].get(name)


def categories() -> tuple[str, ...]:
    """The categories, sorted: "Account / User", "Agriculture" …"""
    return _load()[2]


def _word_rank(symbol: Symbol, tokens: list[str]) -> int | None:
    """2 if every token starts a word of the name, 3 if also of the aliases,
    4 if also of the categories; None if one starts no word at all."""
    best = 0
    for token in tokens:
        for rank, words in enumerate(symbol.words, 2):
            if any(word.startswith(token) for word in words):
                best = max(best, rank)
                break
        else:
            return None
    return best


def _rank(symbol: Symbol, tokens: list[str]) -> int | None:
    """How well a symbol matches (lower is better), None if not at all:
    0 the name itself ("arrow left" or "arrowleft" for arrow-left), 1 a name
    that adds words to it, else _word_rank(); the words written together
    also count ("wi fi", "wi-fi" find wifi)."""
    joined, compact = "-".join(tokens), "".join(tokens)
    name = symbol.name
    if name == joined or name.replace("-", "") == compact:
        return 0
    if name.startswith(joined + "-"):
        return 1
    rank = _word_rank(symbol, tokens)
    if rank is None and len(tokens) > 1:
        rank = _word_rank(symbol, [compact])
    return rank


def search(query: str, category: str | None = None) -> list[Symbol]:
    """The symbols matching every word of the query (a word matches the start
    of a word in a symbol's name, aliases or categories), best first (see
    _rank, then shorter names). An empty query lists all of the category (or
    all symbols); one without letters or digits ("--") finds nothing."""
    candidates = [s for s in all_symbols() if category is None or category in s.categories]
    text = query.strip().lower()
    if not text:
        return candidates
    tokens = _WORD.findall(text)
    if not tokens:
        return []
    ranked = []
    for symbol in candidates:
        rank = _rank(symbol, tokens)
        if rank is not None:
            # equally good: the shorter name (fewer extra words) first
            ranked.append((rank, symbol.name.count("-"), symbol.name, symbol))
    ranked.sort(key=lambda item: item[:3])
    return [item[3] for item in ranked]


def remember(recent: object, name: str) -> list[str]:
    """The recently used names with name in front (from settings, which may
    hold anything): the last MAX_RECENT, each once."""
    names = [n for n in recent if isinstance(n, str)] if isinstance(recent, list) else []
    return [name, *(n for n in names if n != name)][:MAX_RECENT]


@lru_cache(maxsize=512)
def _parsed(path: str) -> tuple[Segment, ...] | PathError:
    try:
        return tuple(parse(path))
    except PathError as error:
        return error


def segments(path: str) -> tuple[Segment, ...]:
    """The parsed path, cached (also a failure): the chooser draws hundreds
    of symbols, the preview the same ones again and again. PathError if it
    is invalid."""
    result = _parsed(path)
    if isinstance(result, PathError):
        raise PathError(str(result))
    return result


def trace(ctx, path_segments) -> None:
    """Add the segments to a cairo context's path."""
    for segment in path_segments:
        command, *args = segment
        if command == "M":
            ctx.move_to(*args)
        elif command == "L":
            ctx.line_to(*args)
        elif command == "C":
            ctx.curve_to(*args)
        else:
            ctx.close_path()
