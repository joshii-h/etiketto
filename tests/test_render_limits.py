"""The render limits of the print server (release review of 1.1.0): the free
layout, texts and codes as they are filled in, and pages of shipping labels
(image formats, the pixel budget, no caches, PDFs in a child process)."""

from __future__ import annotations

import base64
import io
import json
import signal
import subprocess
import sys

import pytest
from PIL import Image
from shipping_samples import a4_pdf
from test_server import call, label_file, wait_for

from tavletta import imagefx, typeset
from tavletta import render as render_module
from tavletta.model import CodeElement, ImageElement, Label, PageElement, TextElement
from tavletta.render import RenderTooLarge, limited, render
from tavletta.server import main as server_main
from tavletta.server import service as service_module
from tavletta.shipping import pdfchild, source
from tavletta.shipping.source import SourceError


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def png(size, mode="RGB") -> str:
    buffer = io.BytesIO()
    Image.new(mode, size, "white").save(buffer, format="PNG")
    return b64(buffer.getvalue())


def encoded(image: Image.Image, fmt: str) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt)
    return b64(buffer.getvalue())


def has_poppler() -> bool:
    try:
        source._poppler()
    except SourceError:
        return False
    return True


needs_poppler = pytest.mark.skipif(not has_poppler(), reason="Poppler (poppler-glib) missing")


@pytest.fixture(autouse=True)
def _fresh_caches():
    source.clear_caches()
    yield
    source.clear_caches()


@pytest.fixture(scope="module")
def pdf():
    return b64(a4_pdf("top-left", pages=2))


@pytest.fixture
def made(monkeypatch):
    """The sizes of the images Pillow makes with Image.new meanwhile."""
    sizes = []
    real = Image.new

    def new(mode, size, *args, **kwargs):
        sizes.append(tuple(size))
        return real(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", new)
    return sizes


def free(*elements, **fields) -> Label:
    fields.setdefault("tape_mm", 12)
    return Label(elements=list(elements), layout="free", **fields)


# --- H1: the free layout -----------------------------------------------------------


def test_free_layout_code_is_checked_before_its_mask_is_made(made):
    """A PDF417 of 10 mm modules: refused before its mask (gigabytes at
    the server's limits) is made; RenderTooLarge passes _part()."""
    code = CodeElement(data="x" * 40, symbology="pdf417", module_mm=10)
    with limited(1_000_000), pytest.raises(RenderTooLarge):
        render(free(code), 300)
    assert all(w * h <= 1_000_000 for w, h in made)


def test_free_layout_budget_is_counted_as_elements_are_drawn(monkeypatch):
    drawn = []
    real = render_module.render_image

    def counting(*args, **kwargs):
        drawn.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(render_module, "render_image", counting)
    data = png((10, 10))
    images = [ImageElement(data=data, height_mm=10) for _n in range(16)]
    with limited(30_000), pytest.raises(RenderTooLarge):  # each 118², two too many
        render(free(*images), 300)
    assert len(drawn) == 3  # not all 16 first
    assert render(free(*images), 300).image.width > 0  # without limits


# --- H2: texts and codes as they are filled in -------------------------------------


def test_expanded_text_is_refused_before_pango(monkeypatch):
    laid_out = []
    real = typeset._attributes

    def watched(*args):
        laid_out.append(1)  # Pango is about to get the text
        return real(*args)

    monkeypatch.setattr(typeset, "_attributes", watched)
    for text in ("W" * 5001, "W\n" * 100 + "W", "a\nb" * 100):
        with limited(10**9), pytest.raises(RenderTooLarge):
            render(Label(tape_mm=12, elements=[TextElement(text=text)]), 100)
        with limited(10**9), pytest.raises(RenderTooLarge):
            render(free(TextElement(text=text)), 100)
    assert laid_out == []
    # at the limits, and without limits at all
    with limited(10**9):
        render(Label(tape_mm=12, elements=[TextElement(text="W\n" * 99 + "W")]), 100)
    assert render(Label(tape_mm=12, elements=[TextElement(text="W" * 5001)]), 50)


def test_expanded_code_is_refused():
    code = CodeElement(data="1" * 5001, symbology="qr")
    with limited(10**9), pytest.raises(RenderTooLarge):
        render(Label(tape_mm=12, elements=[code]), 100)
    with limited(10**9), pytest.raises(RenderTooLarge):
        render(free(code), 100)


def test_check_text():
    render_module.check_text("x" * 10**6)  # no limits
    with limited(10**9):
        render_module.check_text("x\n" * 99)  # 100 lines
        render_module.check_text("x\n" * 200, lines=False)
        with pytest.raises(RenderTooLarge):
            render_module.check_text("x\n" * 100)


@pytest.fixture
def api(make_server):
    server, base = make_server()
    return server, base


def content(*elements, **fields):
    fields.setdefault("printer", "fake-mono")
    fields.setdefault("tape_mm", 12)
    return Label(elements=list(elements), **fields).to_dict()


@pytest.mark.parametrize(
    "body",
    [
        {"label_content": content(TextElement(text="{=a}" * 1250)), "set": {"a": "W" * 5000}},
        {"label_content": content(TextElement(text="{=a}" * 3)), "set": {"a": "W\n" * 99}},
        {
            "label_content": content(TextElement(text="{Name}" * 3)),
            "data_rows": [{"Name": "x\n" * 99}],
        },
        {"label_content": content(CodeElement(data="{=a}{=a}")), "set": {"a": "1" * 3000}},
    ],
)
def test_filled_in_texts_are_refused_over_http(api, body):
    _server, base = api
    status, _r, answer = call(base, "POST", "/api/v1/preview", body=body)
    assert status == 413 and answer["error"]["code"] == "label_too_large", answer


def test_every_row_of_a_series_is_within_the_limits(api, server_env):
    server, base = api
    rows = [{"Name": "fine"}, {"Name": "x\n" * 99}]
    body = {
        "label_content": content(TextElement(text="{Name}{Name}")),
        "data_rows": rows,
        "wait": 10,
    }
    status, _r, job = call(base, "POST", "/api/v1/print", body=body)
    assert status in (200, 202), job
    wait_for(lambda: server.service.job(job["id"]).state in ("failed", "done"))
    assert server.service.job(job["id"]).state == "failed"


def test_max_lines_counts_lines_not_newlines():
    assert service_module._too_long("W\n" * 100)  # 101 lines
    assert not service_module._too_long("W\n" * 99)


# --- H3: pages of shipping labels --------------------------------------------------


@pytest.mark.parametrize("fmt", ["ICO", "PCX", "TGA", "SGI", "DDS"])
def test_pages_take_only_the_safe_image_formats(fmt):
    data = encoded(Image.new("RGB", (16, 16), "black"), fmt)
    with pytest.raises(SourceError) as error:
        source.page_info(data)
    assert error.value.code == "unreadable"


def test_eps_is_never_run(monkeypatch):
    from PIL import EpsImagePlugin

    ran = []

    def ghostscript(*args, **_kwargs):
        ran.append(args)
        raise OSError("no Ghostscript in tests")

    monkeypatch.setattr(EpsImagePlugin, "Ghostscript", ghostscript)
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\nshowpage\n"
    with pytest.raises(SourceError):
        source.draw(b64(eps), 0, (0, 0, 1, 1), 0, (10, 10))
    label = Label(tape_mm=12, elements=[PageElement(data=b64(eps))])
    assert render(label, 100).warnings[0].key == "page_unreadable"
    assert ran == []


def test_image_pages_count_in_the_image_budget():
    page = PageElement(data=png((300, 200)))
    label = Label(tape_mm=12, elements=[page])
    with limited(100_000), pytest.raises(RenderTooLarge):  # 60 000 > 50 000
        render(label, 100)
    # an image and a page of the same file: decoded apart, counted apart
    both = Label(tape_mm=12, elements=[PageElement(data=png((150, 200))), ImageElement()])
    both.elements[1].data = both.elements[0].data
    with limited(100_000), pytest.raises(RenderTooLarge):
        render(both, 100)
    with limited(200_000):
        assert render(both, 100).warnings == []


def test_a_larger_frame_than_counted_is_refused(monkeypatch):
    frames = [Image.new("L", (10, 10)), Image.new("L", (300, 300))]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
    page = PageElement(data=b64(buffer.getvalue()), page=1)
    with limited(100_000), pytest.raises(RenderTooLarge):
        render(Label(tape_mm=12, elements=[page]), 100)
    with limited(100_000), pytest.raises(SourceError):
        source._limited_picture("not base64!", 0, render_module.active_limits())


def test_nothing_of_a_page_is_kept_within_limits():
    label = Label(tape_mm=12, elements=[PageElement(data=png((40, 60)))])
    with limited(10**7):
        render(label, 100)
        render(label, 100, mono=True)
    assert len(source._pictures) == 0 and len(source._drawings) == 0
    render(label, 100, mono=True)  # the window keeps them
    assert len(source._pictures) == 1 and len(source._drawings) > 0


def test_background_brightness_is_not_cached_within_limits():
    render_module._image_brightness_cached.cache_clear()
    label = Label(tape_mm=12, background_image=ImageElement(data=png((20, 20))))
    with limited(10**7):
        render(label, 100, mono=True)
    assert render_module._image_brightness_cached.cache_info().currsize == 0
    render(label, 100, mono=True)
    assert render_module._image_brightness_cached.cache_info().currsize == 1


@needs_poppler
def test_pdf_pages_are_drawn_by_a_child_within_limits(pdf, monkeypatch):
    label = Label(printer="cab", tape_mm=50, elements=[PageElement(data=pdf)])
    plain = render(label, 300).image
    source.clear_caches()
    asked = []
    real = pdfchild.ask

    def counting(raw, request):
        asked.append(request["op"])
        return real(raw, request)

    monkeypatch.setattr(pdfchild, "ask", counting)
    monkeypatch.setattr(pdfchild, "SECONDS", 120.0)  # a busy test machine
    monkeypatch.setattr(source, "_open_pdf", lambda raw: pytest.fail("Poppler in the server"))
    with limited(40_000_000):
        assert render(label, 300).image.tobytes() == plain.tobytes()
        mono = render(label, 300, mono=True)
    assert asked == ["draw", "info", "draw"] and mono.warnings == []
    assert len(source._drawings) == 0


@needs_poppler
def test_pdf_limits_within_the_child(pdf, monkeypatch):
    label = Label(printer="cab", tape_mm=50, elements=[PageElement(data=pdf)])
    monkeypatch.setattr(source, "LIMITED_PDF_BYTES", 100)
    with limited(40_000_000), pytest.raises(RenderTooLarge):
        render(label, 300)
    monkeypatch.undo()
    with limited(1000), pytest.raises(RenderTooLarge):  # endless tape: the child checks
        source.draw(pdf, 0, (0, 0, 1, 1), 0, (None, 500))
    damaged = b64(b"%PDF-1.4\nnothing else")
    with limited(40_000_000):
        assert render(Label(tape_mm=12, elements=[PageElement(data=damaged)]), 100).warnings
        with pytest.raises(SourceError):
            source.page_info(damaged)


def fake_run(returncode, stdout=b""):
    def run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], returncode, stdout, b"")

    return run


def test_child_outcomes(monkeypatch):
    monkeypatch.setattr(pdfchild.subprocess, "run", fake_run(-signal.SIGXCPU))
    with pytest.raises(pdfchild.ChildLimit):
        pdfchild.ask(b"", {})
    with limited(10**6), pytest.raises(RenderTooLarge):
        source.page_info(b64(b"%PDF-1.4"))
    monkeypatch.setattr(pdfchild.subprocess, "run", fake_run(-signal.SIGSEGV))
    with pytest.raises(pdfchild.ChildFailed):
        pdfchild.ask(b"", {})
    with limited(10**6), pytest.raises(SourceError):
        source.page_info(b64(b"%PDF-1.4"))
    monkeypatch.setattr(pdfchild.subprocess, "run", fake_run(0, b"not json"))
    with pytest.raises(pdfchild.ChildFailed):
        pdfchild.ask(b"", {})
    answer = json.dumps({"width": 3, "height": 3}).encode() + b"\n" + b"short"
    monkeypatch.setattr(pdfchild.subprocess, "run", fake_run(0, answer))
    with limited(10**6), pytest.raises(SourceError):
        source.draw(b64(b"%PDF-1.4"), 0, (0, 0, 1, 1), 0, (3, 3))
    too_large = json.dumps({"error": "too_large"}).encode() + b"\n"
    monkeypatch.setattr(pdfchild.subprocess, "run", fake_run(0, too_large))
    with limited(10**6), pytest.raises(RenderTooLarge):
        source.page_info(b64(b"%PDF-1.4"))

    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("python", 1)

    monkeypatch.setattr(pdfchild.subprocess, "run", timeout)
    with pytest.raises(pdfchild.ChildLimit):
        pdfchild.ask(b"", {})

    def missing(*_args, **_kwargs):
        raise FileNotFoundError("python")

    monkeypatch.setattr(pdfchild.subprocess, "run", missing)
    with pytest.raises(pdfchild.ChildFailed):
        pdfchild.ask(b"", {})


@needs_poppler
def test_child_main_in_process(monkeypatch, capsysbinary):
    """What the child does, here (its limits not set on the tests)."""
    limits = []
    monkeypatch.setattr(pdfchild, "_limit", limits.append)
    raw = a4_pdf("top-left")

    def run(request):
        stdin = io.BytesIO(json.dumps(request).encode() + b"\n" + raw)
        monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(stdin))
        assert pdfchild.main() == 0
        head, _newline, body = capsysbinary.readouterr().out.partition(b"\n")
        return json.loads(head), body

    info, _body = run({"op": "info", "page": 0})
    assert info["pages"] == 1 and info["width"] > 0
    drawing = {"op": "draw", "page": 0, "crop": [0, 0, 1, 1], "rotation": 90, "fit": "fit"}
    answer, body = run({**drawing, "box": [40, 50], "sharp": True, "max_pixels": 10**6})
    assert len(body) == answer["width"] * answer["height"] * 3
    answer, _body = run({**drawing, "box": [400, 500], "sharp": False, "max_pixels": 100})
    assert answer["error"] == "too_large"
    raw = b"%PDF-1.4 damaged"
    answer, _body = run({"op": "info", "page": 0})
    assert answer["error"] == "unreadable"
    assert limits == [0, 10**6, 100, 0]

    def exhausted(_request, _raw):
        raise MemoryError

    monkeypatch.setattr(pdfchild, "_answer", exhausted)
    assert run({"op": "info"})[0]["error"] == "too_large"


def test_child_limits_its_own_memory_and_time():
    """The child sets its limits on itself (run here in a process of its own)."""
    code = (
        "import resource; from tavletta.shipping import pdfchild; pdfchild._limit(1000);"
        "print(resource.getrlimit(resource.RLIMIT_AS)[0], resource.getrlimit(resource.RLIMIT_CPU)[0])"
    )
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, check=True, text=True)
    memory, cpu = map(int, done.stdout.split())
    assert memory == pdfchild._memory(1000) and cpu == pdfchild.CPU_SECONDS


def test_pdfchild_memory():
    assert pdfchild._memory(40_000_000) > pdfchild.BASE_MEMORY


# --- the server: pages in label_content ----------------------------------------------


def test_pages_in_label_content_are_refused_by_default(api, server_env):
    server, base = api
    body = {"label_content": content(PageElement(data=png((20, 20))))}
    for path in ("/api/v1/preview", "/api/v1/print"):
        status, _r, answer = call(base, "POST", path, body=body)
        assert status == 422 and answer["error"]["code"] == "pages_not_allowed", answer
    # a label of the labels folder may hold one
    label_file(server_env.labels, "page.tavletta", PageElement(data=png((20, 20))))
    assert call(base, "POST", "/api/v1/preview", body={"label": "page"})[0] == 200
    server.service.allow_pdf_pages = True
    assert call(base, "POST", "/api/v1/preview", body=body)[0] == 200


def test_pages_count_as_pictures(api):
    server, base = api
    server.service.allow_pdf_pages = True
    pages = [PageElement(data=png((4, 4))) for _n in range(51)]
    status, _r, answer = call(
        base, "POST", "/api/v1/preview", body={"label_content": content(*pages)}
    )
    assert status == 413 and answer["error"]["code"] == "label_too_large"


def test_allow_pdf_pages_option(tmp_path):
    from tavletta.cli import parser

    for command in ("serve", "mcp"):
        args = parser().parse_args([command, "--labels-dir", str(tmp_path)])
        assert args.allow_pdf_pages is False
        args = parser().parse_args([command, "--labels-dir", str(tmp_path), "--allow-pdf-pages"])
        service = server_main._service(args, None)
        try:
            assert service.allow_pdf_pages
        finally:
            service.close()


# --- nits ------------------------------------------------------------------------------


def test_picture_kind():
    assert imagefx.picture_kind(Image.new("L", (10, 10), 0)) == "drawing"
    tones = Image.new("L", (30, 10), 60)
    tones.paste(120, (10, 0, 20, 10))
    tones.paste(180, (20, 0, 30, 10))
    assert imagefx.picture_kind(tones) == "tones"
    gradient = Image.linear_gradient("L").resize((64, 64))
    assert imagefx.picture_kind(gradient) == "photo"
