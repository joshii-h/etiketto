"""A PDF page read and drawn by Poppler in a process of its own, for the
print server: a PDF a client sends is parsed outside the server's process,
within limits of memory (RLIMIT_AS), processor time (RLIMIT_CPU) and wall
time. A PDF that breaks them refuses the label (RenderTooLarge), a damaged
one or a crash is unreadable; the server goes on either way.

The request goes to the child's standard input as one line of JSON and
the PDF after it; the answer comes back as one line of JSON (the page's
size, or an error) and, for a drawing, its RGB pixels after it.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess  # nosec B404
import sys
from pathlib import Path

SECONDS = 20.0  # wall time of one child
CPU_SECONDS = 10
# address space: Python, PyGObject, Poppler and cairo (about 60 MB), the
# PDF and what Poppler makes of it, plus a drawing's surfaces and copies
# (two cairo surfaces, Pillow copies, the ink mask, the pixels sent back)
BASE_MEMORY = 512 * 2**20
MEMORY_PER_PIXEL = 24
# killed by its limits: refused as too large rather than as damaged
LIMIT_SIGNALS = (signal.SIGXCPU, signal.SIGKILL)


class ChildLimit(Exception):
    """The child broke its limits (time, memory)."""


class ChildFailed(Exception):
    """The child failed otherwise (a crash in Poppler, Python missing)."""


def _memory(max_pixels: int) -> int:
    return BASE_MEMORY + MEMORY_PER_PIXEL * max_pixels


def ask(raw: bytes, request: dict) -> tuple[dict, bytes]:
    """The child's answer to request (op "info" or "draw", see _answer())
    about the PDF raw, and the pixels of a drawing."""
    root = str(Path(__file__).resolve().parent.parent.parent)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (root, env.get("PYTHONPATH", "")) if p)
    try:
        # this module only, a fixed command line, no shell
        done = subprocess.run(  # nosec B603
            [sys.executable, "-m", __name__],
            input=json.dumps(request).encode() + b"\n" + raw,
            capture_output=True,
            timeout=SECONDS,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ChildLimit("the PDF took more than %.0f s" % SECONDS) from None
    except OSError as e:
        raise ChildFailed(str(e)) from None
    if done.returncode < 0 and -done.returncode in LIMIT_SIGNALS:
        raise ChildLimit("the PDF took too long (signal %d)" % -done.returncode)
    head, _newline, body = done.stdout.partition(b"\n")
    try:
        answer = json.loads(head)
    except ValueError:
        answer = None
    if done.returncode != 0 or not isinstance(answer, dict):
        raise ChildFailed("the child ended with %d" % done.returncode)
    return answer, body


def _limit(max_pixels: int) -> None:
    import resource

    memory = _memory(max_pixels)
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (CPU_SECONDS, CPU_SECONDS + 1))


def _answer(request: dict, raw: bytes) -> tuple[dict, bytes]:
    from tavletta.shipping import source

    pdf_page, count = source._pdf_page(source._open_pdf(raw), int(request.get("page", 0)))
    width, height = pdf_page.get_size()
    if width <= 0 or height <= 0:
        raise source.SourceError("unreadable", "page without a size")
    if request.get("op") == "info":
        return {"width": width, "height": height, "pages": count}, b""
    rotation = int(request["rotation"])
    region = source.area(request["crop"], width, height)
    box = (request["box"][0], int(request["box"][1]))
    size_w, size_h, scale = source.fitted(
        source.turned(region[2], region[3], rotation), box, request["fit"]
    )
    if size_w * size_h > int(request["max_pixels"]):
        return {"error": "too_large", "detail": "%d × %d pixels" % (size_w, size_h)}, b""
    image = source._draw_pdf(
        pdf_page, region, rotation, (size_w, size_h), scale, bool(request["sharp"])
    )
    return {"width": size_w, "height": size_h}, image.tobytes()


def main() -> int:
    stdin = sys.stdin.buffer
    request = json.loads(stdin.readline())
    _limit(int(request.get("max_pixels", 0)))
    raw = stdin.read()
    from tavletta.shipping.source import SourceError

    try:
        answer, body = _answer(request, raw)
    except SourceError as e:
        answer, body = {"error": e.code, "detail": str(e)}, b""
    except MemoryError:
        answer, body = {"error": "too_large", "detail": "out of memory"}, b""
    out = sys.stdout.buffer
    out.write(json.dumps(answer).encode() + b"\n")
    out.write(body)
    out.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
