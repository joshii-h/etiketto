"""Label document model and its JSON (.etiketto) format."""

from __future__ import annotations

import base64
import contextlib
import errno
import fcntl
import json
import os
import secrets
import stat
import time
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

# Version 1: every label. Version 2: the file lists "features" it needs; an
# Etiketto that does not know one of them refuses the file ("newer") instead
# of printing it wrongly. A file is written with version 2 only if it uses a
# feature, so labels without one still open in every version. New features
# add a name to FEATURES, not a version.
FORMAT_VERSION = 2
# "variables": {=date}, {=counter} … in texts and codes, and the counter
# fields; older versions would print "{=date}" as it is and drop the counter
# when they save
FEATURES = frozenset({"variables"})

DEFAULT_PRINTER = "vc500w"
MIN_TAPE_LENGTH_MM = 15.0  # endless tape: the shortest fixed length a file may have
# die-cut labels: smallest and largest side a file may have
MIN_SIZE_MM, MAX_SIZE_MM = 5.0, 1000.0
MAX_GAP_MM = 50.0
# {=counter}: up to 15 digits stay exact in the interface's number fields
MAX_COUNTER = 10**15 - 1
MAX_COUNTER_STEP = 10**6
# a label file is read whole before it is parsed: refuse absurd sizes (the
# json documentation advises a limit for data from untrusted sources)
MAX_FILE_BYTES = 200 * 1024 * 1024
# what a printer model decides about a label: kept per model when switching
GEOMETRY_FIELDS = ("tape_mm", "auto_length", "length_mm", "orientation", "gap_mm")


class LabelError(ValueError):
    """A file that cannot be used as a label. code: "not_label" (not an
    Etiketto file at all), "newer" (written by a newer Etiketto) or "damaged"
    (an Etiketto file with invalid content); the message is English detail
    for bug reports, the interface shows its own translated text."""

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def _damaged(detail: str) -> LabelError:
    return LabelError("damaged", detail)


@dataclass
class TextElement:
    text: str = "Text"
    font: str = "Sans"  # fontconfig family
    bold: bool = True
    italic: bool = False
    size_pt: float = 0.0  # 0 = fit to tape height
    color: str = "#000000"
    align: Literal["left", "center", "right"] = "left"
    # for the command line: `etiketto print --set NAME=VALUE` replaces the text
    name: str = ""
    kind: Literal["text"] = "text"


@dataclass
class ImageElement:
    data: str = ""  # base64 encoded image file, keeps documents self-contained
    name: str = ""  # original file name, for display only
    kind: Literal["image"] = "image"

    @classmethod
    def from_file(cls, path: str | Path) -> ImageElement:
        path = Path(path)
        return cls(data=base64.b64encode(path.read_bytes()).decode(), name=path.name)

    def raw(self) -> bytes:
        return base64.b64decode(self.data)


@dataclass
class CodeElement:
    data: str = "https://"
    symbology: Literal["qr", "code128", "ean13"] = "qr"
    color: str = "#000000"
    name: str = ""  # see TextElement.name
    kind: Literal["code"] = "code"


Element = TextElement | ImageElement | CodeElement
ELEMENT_TYPES = {"text": TextElement, "image": ImageElement, "code": CodeElement}


@dataclass
class Label:
    # Label space: the length runs along x, the elements are placed one after
    # another along it; tape_mm is the height across. On endless tape tape_mm
    # is the tape width; on die-cut labels both together are the label size
    # (see media_size()).
    tape_mm: float = 50
    auto_length: bool = True  # endless tape only
    length_mm: float = 60.0  # used when auto_length is False
    margin_mm: float = 2.0  # space at both ends and between elements
    edge_mm: float = 0.0  # extra space along the tape edges (on top of the safety margin)
    align: Literal["start", "center", "end"] = "center"  # fixed length only
    background: str = "#ffffff"
    background_image: ImageElement | None = None  # covers the whole label
    border_mm: float = 0.0
    border_color: str = "#000000"
    elements: list[Element] = field(default_factory=list)
    printer: str = DEFAULT_PRINTER  # backend id; files without it are for the VC-500W
    # die-cut labels only: the gap to the next label, and whether the content
    # runs across the roll (along the print head) or along it like on tape
    gap_mm: float = 3.0
    orientation: Literal["across", "along"] = "along"
    # {=counter} (see variables.py): the number of the next printed label and
    # how much it goes up per label; printing moves counter_next on
    counter_next: int = 1
    counter_step: int = 1

    # --- die-cut labels ----------------------------------------------------

    def media_size(self) -> tuple[float, float]:
        """(width across the print head, height along the feed) in mm."""
        if self.orientation == "across":
            return self.length_mm, self.tape_mm
        return self.tape_mm, self.length_mm

    def set_media_size(self, width_mm: float, height_mm: float) -> None:
        if self.orientation == "across":
            self.length_mm, self.tape_mm = width_mm, height_mm
        else:
            self.tape_mm, self.length_mm = width_mm, height_mm

    def set_orientation(self, orientation: str) -> None:
        """Turn the content, the label size stays."""
        width, height = self.media_size()
        self.orientation = "across" if orientation == "across" else "along"
        self.set_media_size(width, height)

    def pitch_mm(self) -> float:
        """Distance from the start of one die-cut label to the next."""
        return self.media_size()[1] + self.gap_mm

    def use_default_size(self, caps) -> None:
        """A die-cut label of the printer's default size, content across the roll."""
        # a plugin without sizes of its own: 50 × 25 mm, entered by hand
        size = caps.label_sizes[0] if caps.label_sizes else None
        self.auto_length = False
        self.orientation = "across"
        self.set_media_size(*((size.width_mm, size.height_mm) if size else (50.0, 25.0)))

    # --- printer models ------------------------------------------------------

    @classmethod
    def new_for(cls, printer: str, inserted_tape: float | None = None) -> Label:
        """An empty label for a printer model: on tape the inserted (or widest
        common) tape, die-cut labels in the default size."""
        caps = _capabilities(printer)
        label = cls(printer=printer)
        if caps.continuous:
            widths = caps.tape_widths_mm
            fallback = 50 if 50 in widths else widths[-1]
            label.tape_mm = (
                inserted_tape if inserted_tape in widths and inserted_tape else fallback
            )
        else:
            label.use_default_size(caps)
        return label

    def geometry(self) -> dict:
        return {name: getattr(self, name) for name in GEOMETRY_FIELDS}

    def switch_printer(self, printer: str, geometry: dict | None = None) -> None:
        """Move the label to another printer model. `geometry` is what it had
        on that model before (see geometry()); without it the label is made
        to fit: the nearest tape, or the default label size."""
        was_continuous = _capabilities(self.printer).continuous
        caps = _capabilities(printer)
        self.printer = printer
        if geometry:
            for name, value in geometry.items():
                setattr(self, name, value)
        elif caps.continuous:
            if not was_continuous:  # the content runs along the tape again
                self.orientation = "along"
                self.auto_length = True
            self.tape_mm = caps.nearest_tape(self.tape_mm)  # 50, not 50.0
        elif was_continuous:
            self.use_default_size(caps)
        self._page_upright(caps)

    def _page_upright(self, caps) -> None:
        """A page has no roll the content could run along, and the interface
        offers no direction: the content runs across, the size stays."""
        if caps.pages and self.orientation != "across":
            self.set_orientation("across")

    # --- persistence -------------------------------------------------------

    def features(self) -> list[str]:
        """What a reader must understand to print this label as meant."""
        return ["variables"] if self._uses_variables() else []

    def _uses_variables(self) -> bool:
        if (self.counter_next, self.counter_step) != (1, 1):
            return True
        return any(
            "{=" in (e.data if isinstance(e, CodeElement) else e.text)
            for e in self.elements
            if isinstance(e, (TextElement, CodeElement))
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        features = self.features()
        data["version"] = FORMAT_VERSION if features else 1
        if features:
            data["features"] = features
        return data

    @classmethod
    def from_dict(cls, data: dict) -> Label:
        """Build a label from untrusted JSON: unknown fields are ignored,
        wrong types raise LabelError, numbers are clamped to sane ranges."""
        if not isinstance(data, dict):
            raise LabelError("not_label", "not an Etiketto file")
        data = dict(data)
        version = data.pop("version", FORMAT_VERSION)
        if isinstance(version, bool) or not isinstance(version, int):
            raise _damaged("invalid format version %r" % (version,))
        if version > FORMAT_VERSION:
            raise LabelError("newer", "file was written by a newer Etiketto (format %s)" % version)
        features = data.pop("features", [])
        if not isinstance(features, list) or not all(isinstance(f, str) for f in features):
            raise _damaged("features must be a list of names")
        if unknown := sorted(set(features) - FEATURES):
            raise LabelError("newer", "file needs features of a newer Etiketto: %s" % unknown)
        items = data.pop("elements", [])
        if not isinstance(items, list):
            raise _damaged("elements must be a list")
        elements = [_element_from_dict(item) for item in items]
        background_image = data.pop("background_image", None)
        label = cls(**_known_fields(cls, data, frozenset({"elements", "background_image"})))
        label.elements = elements
        if background_image:
            if not isinstance(background_image, dict):
                raise _damaged("background_image must be an object")
            image = _element_from_dict(dict(background_image, kind="image"))
            assert isinstance(image, ImageElement)
            label.background_image = image
        label._validate()
        return label

    def _validate(self) -> None:
        self.printer = _str(self.printer, "printer")
        caps = _capabilities(self.printer)
        continuous = caps.continuous
        if continuous:
            widths = caps.tape_widths_mm
            if isinstance(self.tape_mm, bool) or self.tape_mm not in widths:
                raise _damaged("unsupported tape width %r mm" % (self.tape_mm,))
            self.tape_mm = widths[widths.index(self.tape_mm)]  # 25, not 25.0
        else:
            self.tape_mm = _clamp(self.tape_mm, MIN_SIZE_MM, MAX_SIZE_MM, "tape_mm")
        # a die-cut label has its size, it does not grow with the content
        self.auto_length = bool(self.auto_length) and continuous
        lower = MIN_TAPE_LENGTH_MM if continuous else MIN_SIZE_MM
        self.length_mm = _clamp(self.length_mm, lower, MAX_SIZE_MM, "length_mm")
        self.gap_mm = _clamp(self.gap_mm, 0, MAX_GAP_MM, "gap_mm")
        self.orientation = _choice(self.orientation, ("across", "along"), "orientation")
        if not continuous and caps.max_width_mm:
            # no wider than the print head: also keeps the render of an
            # untrusted file small
            width, height = self.media_size()
            self.set_media_size(min(width, caps.max_width_mm), height)
        self._page_upright(caps)  # a file saved that way by hand or an older version
        self.margin_mm = _clamp(self.margin_mm, 0, 20, "margin_mm")
        self.edge_mm = _clamp(self.edge_mm, 0, 10, "edge_mm")
        self.border_mm = _clamp(self.border_mm, 0, 5, "border_mm")
        self.align = _choice(self.align, ("start", "center", "end"), "align")
        self.background = _str(self.background, "background")
        self.border_color = _str(self.border_color, "border_color")
        self.counter_next = _int(self.counter_next, 0, MAX_COUNTER, "counter_next")
        self.counter_step = _int(self.counter_step, 1, MAX_COUNTER_STEP, "counter_step")

    def save(self, path: str | Path) -> None:
        write_file(path, json.dumps(self.to_dict(), indent=1, ensure_ascii=False).encode())

    @staticmethod
    def store_counter(path: str | Path, counter_next: int) -> None:
        """Write a new counter_next into a saved label and change nothing else
        (the command line prints with overrides that must not be saved)."""
        data = json.loads(read_file(path).decode("utf-8"))
        if not isinstance(data, dict):
            raise LabelError("not_label", "not an Etiketto file")
        data["counter_next"] = min(max(counter_next, 0), MAX_COUNTER)
        features = data.get("features")
        features = list(features) if isinstance(features, list) else []
        if "variables" not in features:  # see FEATURES
            data["features"] = features + ["variables"]
        version = data.get("version", 1)
        if not isinstance(version, int) or isinstance(version, bool) or version < FORMAT_VERSION:
            data["version"] = FORMAT_VERSION
        write_file(path, json.dumps(data, indent=1, ensure_ascii=False).encode())

    @staticmethod
    def saved_counter(path: str | Path) -> int | None:
        """counter_next as saved in the file; None if it cannot be read."""
        try:
            data = json.loads(read_file(path).decode("utf-8"))
        except (OSError, ValueError, RecursionError):
            return None
        value = data.get("counter_next", 1) if isinstance(data, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return min(max(value, 0), MAX_COUNTER)

    @classmethod
    def load(cls, path: str | Path) -> Label:
        """OSError if the file cannot be read, LabelError for its content."""
        raw = read_file(path)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError) as e:  # no JSON, not UTF-8, absurdly nested
            raise LabelError("not_label", str(e) or type(e).__name__) from e
        return cls.from_dict(data)


# --- files ------------------------------------------------------------------


def read_file(path: str | Path) -> bytes:
    """The content of a label file, read only up to MAX_FILE_BYTES. Only a
    regular file: a device like /dev/zero never ends, a FIFO would block
    (opened without blocking to find out)."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        regular = stat.S_ISREG(os.fstat(fd).st_mode)
    except BaseException:
        os.close(fd)
        raise
    if not regular:  # also a folder: closed here, open() would leave it open
        os.close(fd)
        raise LabelError("not_label", "not a regular file")
    with open(fd, "rb") as f:  # owns fd from here on
        raw = f.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise _damaged("file larger than %d MB" % (MAX_FILE_BYTES // 2**20))
    return raw


def write_file(path: str | Path, data: bytes) -> None:
    """Replace the file at `path` with `data` in one step: a crash leaves the
    old or the new file, never half of one.

    - A symbolic link stays a link: the file it points to is replaced.
    - The file keeps its permissions and, where allowed, its owner and group.
    - The temporary file next to it has a random name and is created
      exclusively (O_EXCL, O_NOFOLLOW): a link planted under a predictable
      name cannot redirect the write, and the window and the command line
      writing the same label at once do not share one temporary file.
    - File and directory are flushed to the disk before and after the rename.

    Flatpak: a file opened through the document portal lives on its FUSE
    file system (/run/user/…/doc/). The portal handles the rename of a file
    within its directory, but only that file is exported, not its folder;
    should a portal version refuse the temporary file, saving fails with an
    error and the label stays unchanged. Not testable outside the sandbox.
    """
    target = Path(os.path.realpath(path))
    try:
        old: os.stat_result | None = os.stat(target)
    except FileNotFoundError:
        old = None
    if old and not os.access(target, os.W_OK):  # replacing would get round it
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES), str(path))
    fd, tmp = _create_temporary(target, 0o600 if old else 0o666)  # new: the umask applies
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if old:
                _keep_owner_and_mode(f.fileno(), old)
            with contextlib.suppress(OSError):  # EINVAL/ENOTSUP: no fsync there
                os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    _sync_directory(target.parent)


def writable(path: str | Path) -> bool:
    """Whether write_file(path) can work: it refuses a file without write
    permission, and makes the new file in the folder, so both have to be
    writable. Owner, mode and fsync are best effort, not needed."""
    target = os.path.realpath(path)
    if os.path.exists(target) and not os.access(target, os.W_OK):
        return False
    return os.access(os.path.dirname(target), os.W_OK | os.X_OK)


def _create_temporary(target: Path, mode: int) -> tuple[int, str]:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for _attempt in range(100):
        tmp = str(target.with_name(".%s.%s.tmp" % (target.name, secrets.token_hex(6))))
        try:
            return os.open(tmp, flags, mode), tmp
        except FileExistsError:  # pragma: no cover - 48 random bits
            continue
    raise FileExistsError("no free temporary name next to %s" % target.name)  # pragma: no cover


def _keep_owner_and_mode(fd: int, old: os.stat_result) -> None:
    if (old.st_uid, old.st_gid) != (os.getuid(), os.getgid()):
        # only root may give a file away; a group of one's own works for all
        with contextlib.suppress(OSError):
            os.fchown(fd, old.st_uid, old.st_gid)
        with contextlib.suppress(OSError):
            os.fchown(fd, -1, old.st_gid)
    # best effort: on vfat/NTFS mounts (fixed owner and mode) changing them
    # fails, and the file gets the mount's mode anyway, like a new file
    with contextlib.suppress(OSError):
        os.fchmod(fd, stat.S_IMODE(old.st_mode))


def _sync_directory(directory: Path) -> None:
    """The rename itself on the disk; not every file system can do this."""
    with contextlib.suppress(OSError):
        fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@contextlib.contextmanager
def locked(
    path: str | Path, waiting: Callable[[], None] | None = None, timeout: float = 60.0
) -> Iterator[None]:
    """Hold an exclusive lock (flock) on the label file: two command line
    prints of the same label run one after the other, so no counter value
    is printed twice. write_file() replaces the file, a waiting process
    would then hold a lock on the old one: it checks that the locked file
    is still the one at `path`, else it locks the new one. Without a lock
    (no such file, a file system without flock) it goes on unlocked.

    waiting() is called once if another process holds the lock; after
    `timeout` seconds LockTimeout is raised. The file is opened for
    writing where allowed (flock over NFS needs that), else for reading.
    Anything but a regular file (a FIFO would block the open) is not
    opened here: the load that follows refuses it."""
    target = os.path.realpath(path)
    deadline = time.monotonic() + timeout
    while True:
        fd = _open_for_lock(target)
        if fd is None:
            yield  # the load that follows says what is wrong
            return
        try:
            _lock(fd, waiting, deadline)
            waiting = None
            if _same_inode(fd, target):
                break
        except LockTimeout:
            os.close(fd)
            raise
        except OSError:  # no flock on this file system
            os.close(fd)
            yield
            return
        os.close(fd)  # replaced meanwhile: lock the new file
    try:
        yield
    finally:
        os.close(fd)  # closing releases the lock


class LockTimeout(OSError):
    """Another process held the label file longer than the timeout."""


def _open_for_lock(target: str) -> int | None:
    try:
        if not stat.S_ISREG(os.stat(target).st_mode):
            return None
    except OSError:
        return None
    for flags in (os.O_RDWR, os.O_RDONLY):
        try:
            return os.open(target, flags | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
        except OSError as e:
            if isinstance(e, PermissionError) or e.errno == errno.EROFS:
                continue  # not for writing: for reading then
            return None
    return None


def _lock(fd: int, waiting: Callable[[], None] | None, deadline: float) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    except BlockingIOError:
        if waiting:
            waiting()
    while True:  # poll: flock itself has no timeout
        time.sleep(0.1)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LockTimeout(errno.EWOULDBLOCK, "label file locked") from None


def _same_inode(fd: int, path: str) -> bool:
    try:
        now = os.stat(path)
    except OSError:
        return False
    mine = os.fstat(fd)
    return (mine.st_dev, mine.st_ino) == (now.st_dev, now.st_ino)


# --- validation helpers -----------------------------------------------------


def _capabilities(printer: str):
    """Capabilities of the printer's backend; a printer this version does not
    know means the file comes from a newer Etiketto."""
    from etiketto.backends import get_backend

    try:
        return get_backend(printer).capabilities
    except KeyError:
        raise LabelError("newer", "unknown printer %r" % printer) from None


def _known_fields(cls, data: dict, skip: frozenset[str] = frozenset()) -> dict:
    return {k: v for k, v in data.items() if k in cls.__dataclass_fields__ and k not in skip}


def _clamp(value, lower: float, upper: float, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _damaged("%s must be a number" % name)
    return float(min(upper, max(lower, value)))


def _int(value, lower: int, upper: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _damaged("%s must be a whole number" % name)
    return min(upper, max(lower, value))


def _choice(value, allowed: tuple, name: str):
    if value not in allowed:
        raise _damaged("invalid %s %r" % (name, value))
    return value


def _str(value, name: str) -> str:
    if not isinstance(value, str):
        raise _damaged("%s must be text" % name)
    return value


def _element_from_dict(item) -> Element:
    if not isinstance(item, dict):
        raise _damaged("element must be an object")
    kind = item.get("kind")
    if not isinstance(kind, str) or not kind:  # also no list or object: unhashable
        raise _damaged("element without a valid kind")
    if kind not in ELEMENT_TYPES:  # a kind of element added later
        raise LabelError("newer", "unknown element type %r" % kind)
    cls = ELEMENT_TYPES[kind]
    element = cls(**_known_fields(cls, item))
    if isinstance(element, TextElement):
        element.text = _str(element.text, "text")
        element.font = _str(element.font, "font")
        element.color = _str(element.color, "color")
        element.bold, element.italic = bool(element.bold), bool(element.italic)
        element.size_pt = _clamp(element.size_pt, 0, 300, "size_pt")
        element.align = _choice(element.align, ("left", "center", "right"), "align")
        element.name = _str(element.name, "name")
    elif isinstance(element, ImageElement):
        element.data = _str(element.data, "image data")
        element.name = _str(element.name, "image name")
    else:
        element.data = _str(element.data, "code data")
        element.color = _str(element.color, "color")
        element.name = _str(element.name, "name")
        element.symbology = _choice(element.symbology, ("qr", "code128", "ean13"), "symbology")
    return element
