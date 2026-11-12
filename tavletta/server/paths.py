"""The labels folder: the only place the server reads label and data files.

A name from a request is a path relative to the folder ("shelf/box.tavletta").
It is refused unless it stays inside the folder: no absolute path, no "..",
no hidden file or folder (".ssh"), no NUL, and after following symbolic
links the file must still lie inside the folder (os.path.realpath), so a
link to /etc/passwd placed in the folder is refused too. Label files end
in .tavletta, data files are tables (model.TABLE_SUFFIXES).
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

from tavletta.model import TABLE_SUFFIXES
from tavletta.server.errors import ApiError, invalid_field, not_found
from tavletta.ui.i18n import _

LABEL_SUFFIX = ".tavletta"
MAX_NAME = 1024  # characters of a name in a request
MAX_DEPTH = 8  # folders below the labels folder that are listed
MAX_LISTED = 10_000  # files listed at most


class LabelsDir:
    def __init__(self, root: str | Path):
        self.root = Path(os.path.realpath(root))

    def label(self, name: object) -> Path:
        """The label file `name` (".tavletta" may be left out); 404 if
        there is none, 400 for a name that leaves the folder."""
        if isinstance(name, str) and not name.endswith(LABEL_SUFFIX):
            name += LABEL_SUFFIX
        path = self._inside(name, "label", (LABEL_SUFFIX,))
        if path is None:
            raise not_found("label_not_found", _("No such label: %s") % name)
        return path

    def data(self, name: object) -> Path:
        """The data file `name`, a table in the folder."""
        path = self._inside(name, "data_file", TABLE_SUFFIXES)
        if path is None:
            raise ApiError(404, "data_not_found", _("No such data file: %s") % name)
        return path

    def contains(self, path: str | Path) -> bool:
        """Whether a file lies in the folder, also after following links,
        and in no hidden folder."""
        real = Path(os.path.realpath(path))
        if not real.is_relative_to(self.root) or real == self.root:
            return False
        return not any(part.startswith(".") for part in real.relative_to(self.root).parts)

    def name_of(self, path: str | Path) -> str:
        """The name of a file in the folder, as the API shows it."""
        return Path(path).absolute().relative_to(self.root).as_posix()

    def _inside(self, name: object, field: str, suffixes: tuple[str, ...]) -> Path | None:
        relative = _relative(name, field)
        if relative.suffix.lower() not in suffixes:
            raise invalid_field(field)
        path = self.root / relative
        if not self.contains(path):  # a link out of the folder: as if not there
            return None
        try:
            regular = stat.S_ISREG(os.stat(path).st_mode)
        except OSError:
            return None
        return path if regular else None

    def labels(self) -> Iterator[Path]:
        """The label files in the folder and its subfolders, sorted by name;
        hidden ones and links that lead out of the folder are left out."""
        found: list[Path] = []
        for folder, dirs, files in os.walk(self.root):
            depth = len(Path(folder).relative_to(self.root).parts)
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and depth < MAX_DEPTH)
            found += (Path(folder) / f for f in files if self._listed(Path(folder) / f))
            if len(found) >= MAX_LISTED:
                break
        yield from sorted(found[:MAX_LISTED], key=self.name_of)

    def _listed(self, path: Path) -> bool:
        name = path.name
        return name.endswith(LABEL_SUFFIX) and not name.startswith(".") and self.contains(path)


def _relative(name: object, field: str) -> PurePosixPath:
    if not isinstance(name, str) or not name or len(name) > MAX_NAME or "\x00" in name:
        raise invalid_field(field)
    if "\\" in name:  # no Windows paths, nothing a check could miss
        raise invalid_field(field)
    relative = PurePosixPath(name)
    if relative.is_absolute() or any(
        part in ("..", ".") or part.startswith(".") for part in relative.parts
    ):
        raise invalid_field(field)
    return relative
