# System printer: IPP to CUPS or to the printer

How Etiketto prints on printers the operating system knows. Everything goes
through IPP (Internet Printing Protocol, RFC 8010 and RFC 8011), the
protocol CUPS itself speaks and that every IPP Everywhere printer
understands; AirPrint printers that only take Apple Raster (`image/urf`)
get that (see below). Python's standard library is enough; there is no
dependency on CUPS libraries or drivers.

Markers as in [cab.md](cab.md): **[spec]** from the RFCs or PWG standards,
**[confirmed]** seen on a device, **[assumed]** likely but untested.

## Which printers

- **CUPS queues on this computer**, listed with `CUPS-Get-Printers` at
  `ipp://localhost/`: printers set up in the system settings, including USB
  printers and printers with a vendor driver. CUPS converts the PDF for the
  printer. [spec] [confirmed]
- **IPP printers on the network**, found by mDNS (`_ipp._tcp`,
  `_ipps._tcp`; the TXT key `rp` gives the path, usually `ipp/print`).
  Etiketto talks to them directly, no CUPS queue needed. [spec] [confirmed]
- Printers that CUPS only creates on demand ("temporary queues" for network
  printers) are not in `CUPS-Get-Printers`, but the mDNS search finds the
  printer itself. [confirmed]
- USB printers with IPP-over-USB (`ipp-usb`) appear as IPP printers on
  `localhost`. [assumed]

The host field takes a printer URI (`ipp://…`, `ipps://…`, also `http://`
and `https://` as the CUPS web pages show them), a CUPS queue name
(`HP_LaserJet`; a name without a dot is taken as a queue) or a host name /
IP address with or without port and path (then `ipp://<host>/ipp/print`).
CUPS classes are addressed as CUPS lists them (`/classes/<name>`).

## Printing

- `Get-Printer-Attributes` first: state, `printer-state-reasons`, the
  formats, `print-scaling-supported`, `media-size-supported`,
  `printer-resolution-default` and `-supported`, `copies-supported`,
  `operations-supported` and `urf-supported`. A printer that reports a problem, or whose
  `printer-state` is stopped (a paused CUPS queue: `cupsdisable`), gets no
  job. [spec] [confirmed with a paused CUPS queue]
- The page size comes from the label in millimetres (A4 is exactly 21000 ×
  29700 hundredths of mm). A listed size up to 1 mm off counts, and the
  listed size is what `media-col` asks for (the nearest one; in a custom
  range, the label's own size). If the printer does not list it, the page
  is turned to portrait when that size is listed; otherwise Etiketto says
  so before sending anything, instead of the printer silently using its
  default paper. [spec] [confirmed with an HP LaserJet Pro 4002: 50 × 30
  mm is below its range]
- `Validate-Job` before the first `Print-Job`, with the same attributes
  and no document, if `operations-supported` lists it (0x0004): an
  ignored `media-col` (status 0x0001 or 0x040b with the attribute in the
  unsupported-attributes group) is the paper size message before
  anything prints; an ignored `copies` sends the copies one by one from
  the start. Any other error status means the printer cannot validate
  this job; `Print-Job` then decides. [spec] [confirmed: an HP LaserJet
  Pro 4002 answers 0x0001 with `media-col` for 100 × 50 mm, and 0x0000 for
  A4 with 3 copies; `ippeveprinter` and CUPS queues accept it]
- A printer that answers `Print-Job` with 0x0001
  (successful-ok-ignored-or-substituted-attributes) lists what it ignored
  in the unsupported-attributes group (the fallback for printers without
  `Validate-Job`): an ignored `media-col` would print on its default
  paper, so the job is canceled with the paper size message; an ignored
  `copies` means it prints one, the others follow as below. [spec]
- `Print-Job` with one page in the label's exact size, in the first of
  these formats the printer lists in `document-format-supported` (none
  listed: PDF, CUPS takes it): PDF; PWG Raster, which every IPP
  Everywhere printer must take (`srgb_8`, else `sgray_8` from
  `pwg-raster-document-type-supported`); Apple Raster (URF, AirPrint, see
  below: `SRGB24`, else `W8`); JPEG (quality 95 without chroma
  subsampling). A raster format whose colour spaces Etiketto cannot make
  (only `black_1`, only `DEVCMYK32` …) is passed over; if nothing is
  left, the message "no format" comes before anything is sent, with the
  formats and colour spaces in the details. `media-col`
  carries the size, `print-scaling=none` (if supported) keeps it at 100 %,
  `copies` is the number of labels, `requesting-user-name` the login
  name. [spec]
- Image quality: shipping labels, barcodes and small text must arrive as
  rendered, without grey edges a thermal printer would dither. The PDF
  embeds each page without loss (`FlateDecode`): 1 bit per pixel when the
  page is only black and white, 8-bit `DeviceGray` when it is grey, else
  8-bit `DeviceRGB`; the page exactly the size in millimetres (A4 is
  595.276 × 841.89 pt). A colour page that would be larger than 8 MB
  that way (a photo: an A4 photo is about 15 MB) goes as JPEG
  (`DCTDecode`, quality 95, no chroma subsampling, about 2.4 MB) instead,
  that page only. A mostly white page stays
  small: a black-and-white 4 × 6 inch label is about 2 kB, an A4 page
  filled with large text (grey edges: 8 bits) 40 to 80 kB, where JPEG at
  quality 95 made 300 to 370 kB, with grey noise around every edge.
- The PDF's image resolution: the printer's `printer-resolution-default`
  when 300 dpi would not land on its dots whole, i.e. below 300 dpi, or
  up to 600 dpi without a whole relation to 300 (203 dpi thermal printers
  through CUPS, 400 dpi); the label (or the sheet's pages) is then
  rendered again at that resolution, as for the raster formats. 300 dpi
  otherwise: 600 and 1200 dpi double every pixel exactly, a page that
  cannot be rendered again stays at 300 dpi. Dots per cm count too (8
  dots/mm is 203 dpi); below 100 dpi it is not believed. [confirmed with a CUPS queue with the CUPS sample
  Zebra driver (default 203 dpi) in front of `ippeveprinter`: CUPS
  reports `printer-resolution-default` 203 dpi, Etiketto sends 812 × 1218
  pixels for 4 × 6 inches; through the queue's filter chain (`cupsfilter`
  with its PPD: pdftopdf, Ghostscript, 1-bit raster at 203 dpi) 60 of 60
  one-pixel hairlines arrive, where the 300 dpi PDF lost 27 of them, and
  Code 128 modules arrive 5 dots wide each instead of 5 and 6]
- The image is placed on the page pixel for pixel: at its own size from
  the top left corner (A4 at 300 dpi is 2480.3 pixels wide, the image
  2480) when that is within a pixel of the page, and 0.01 pt smaller,
  centred on that place. Poppler's renderer (Splash: `pdftoppm`,
  cups-filters' Poppler path) resamples an image that exactly fills whole
  device pixels, with grey edges and lost hairlines; with the inset it
  gives back every pixel at the image's resolution, Ghostscript does
  either way. [confirmed with pdftoppm and Ghostscript 10: hairline pages
  of 4 × 6 inches, A4 and 50 × 30 mm at 203 and 300 dpi come back
  identical; before, pdftoppm made 220 080 grey pixels of a 4 × 6 inch
  page at 203 dpi and 409 200 of an A4 page at 300 dpi. Rendered at twice
  the resolution (600 dpi), Splash still smooths the image; Ghostscript
  doubles it exactly]
- Codes (QR, Code 128, EAN-13) are drawn at the resolution they print
  at, never resampled: every module the widest whole number of dots that
  fits the element's designed space (at least one), pure black and white,
  the code centred in its space and up to one dot per module smaller than
  designed; the human-readable text is laid out on its own, without
  anti-aliasing. A code shrunk to fit a fixed length is drawn again at the
  smaller size. (A 0.3 mm module at 300 dpi is 3.54 dots: it prints at 3,
  so the bars take 85 % of the designed width; at 203 dpi a module of
  2.4 dots prints at 2.)
- PWG Raster and Apple Raster (8-bit sRGB or sGray) are made at 300 dpi
  or the next resolution the printer lists
  (`pwg-raster-document-resolution-supported`, the `RS` keywords of
  `urf-supported`; only 100 to 1200 dpi are believed), and the label
  (or the label sheet's pages) is rendered again at that resolution
  instead of resized, so a 203 dpi printer gets 812 × 1218 pixels for
  4 × 6 inches, sharp. Only a page that cannot be rendered again is
  resized: pixel by pixel (nearest neighbour) for a whole multiple such as
  600 dpi, smoothly otherwise. [confirmed with ippeveprinter: a black and
  white 4 × 6 inch label (QR code) arrives with 0 grey pixels as PDF (1-bit
  image, 1.9 kB; Ghostscript renders it at 203, 300 and 600 dpi without a
  grey pixel) and as PWG Raster at 203, 300 and 600 dpi (812 × 1218,
  1200 × 1800, 2400 × 3600 pixels, 2.3 to 3 kB)]
- Copies: if the upper bound of `copies-supported` is below the number of
  labels, PWG Raster and Apple Raster carry the page several times in one
  job (`TotalPageCount`, the URF page count): the page is compressed once
  and its compressed data repeated. A job has at most 10 pages and at most
  64 MB of data (one page at least), since the document is made in
  memory: a noisy A4 page (a photo) is about 26 MB at 300 dpi and about
  91 MB at 600 dpi, a label page a few kB. So 23 labels are jobs of 10, 10
  and 3 pages; PDF and JPEG go as one job per label. The document is made
  once and sent again for every job of the same size. A printer that does
  not list `copies-supported` is trusted to make the copies, unless it
  gets Apple Raster and `urf-supported` has no `CP<n>` (n at least 1).
  [spec] [confirmed: `ippeveprinter -f image/urf` lists `copies-supported`
  1-1 and gets two pages for two labels]
- If a step fails after jobs were sent (a later `Print-Job` refused, a
  job canceled or stopped), every job sent and not seen completed is
  canceled, so that none of them prints later on its own; a job that
  ended (canceled, aborted) or that the watch canceled already is not
  canceled again, and once the printer cannot be reached (timeout,
  connection refused, name does not resolve) the remaining jobs are not
  tried, each would wait 10 s. The message then says how many labels were
  already printed (finished jobs, and the pages a failed job finished), if
  the printer said so, and, if jobs waited in the queue, that these were
  canceled. [spec]
- `Print-Job` answered 0x0507 (server-error-busy, e.g. still busy with the
  last job or with another computer's) makes no job: it is sent again
  every 3 seconds for up to 30 seconds, then the message is "the printer
  is busy" with the number of labels already printed. [spec]
- `Get-Job-Attributes` every second, together with the printer's state;
  the job's own state decides. An answer without a valid `job-state`
  (missing, 0) is a missed answer, not a finished job:
  - completed is done;
  - canceled (by someone at the printer or in the queue), or aborted with
    a `job-canceled-…` reason: "the print job was canceled at the printer
    or in the queue"; aborted otherwise is the generic message, with the
    `job-state-reasons` in the details;
  - a problem the user fixes at the printer and that it then goes on
    from by itself: `media-empty`, `media-needed`, `media-jam`,
    `door-open`, `cover-open`, `interlock-open` while the job prints, or
    as the reason of a processing-stopped job or a stopped printer. The
    interface shows it while printing ("Out of paper – load paper to
    continue") and Etiketto waits up to 5 minutes for each kind of
    problem (out of paper, cover open, jam: other keywords for the same
    kind keep its time) and up to 15 minutes for all of them together,
    both counted since the last page that came out. A page that comes out
    starts both again, so a job that goes on printing while the printer
    reports such a problem (out of paper in another tray) is not canceled.
    Not fixed in time, the job is canceled with that problem. Once it is
    fixed, the clock for progress (below) starts again. If the printer
    does not answer `Get-Printer-Attributes` once, the problem it reported
    last stays: a missed answer neither ends the wait nor restarts it;
  - processing-stopped for any other reason, or without a reason (the
    printer may clear its reason one answer before the job goes on), for
    three answers in a row: the job is canceled with the printer's reason
    (also if the printer does not answer any more);
  - held (`job-state` 4, e.g. `job-hold-until-specified`,
    `cups-held-for-authentication`) for three answers in a row: canceled,
    with a message to check the queue;
  - pending while the printer or queue is stopped (`printer-state` 5, or
    `printer-stopped` in `job-state-reasons`) for three answers: canceled
    as paused;
  - processing while the printer reports another problem Etiketto knows
    (the table below, without a suffix or with `-error`) or is stopped,
    for three answers: canceled with that problem. Unknown keywords do
    not stop a printing job;
  - processing without progress: once `job-impressions-completed` (or
    `job-media-sheets-completed`) has risen above 0, 5 minutes without
    the next page cancel the job, so a long job is not cut short. A
    printer without these counters, or one that keeps them at 0
    (`ippeveprinter` does), gets 5 minutes plus 30 seconds per label for
    the whole job instead;
  - pending (waiting behind other jobs) for 30 seconds, counted from when
    the job last became pending: Etiketto stops watching and says the
    label was sent and prints when its turn comes; the job stays. Further
    jobs are then sent without waiting.
  While a job prints, CUPS reports `connecting-to-device` and
  `cups-waiting-for-job-completed` without a suffix: routine, not a
  problem. A few missed answers (network errors included) are tolerated.
  Only the printer answering 0x0406 (client-error-not-found) for the job
  counts as done: it forgot the job already (some keep no history). A
  refused connection or a name that does not resolve is a missed answer,
  never "printed". [spec] [confirmed through a CUPS queue to
  ippeveprinter, also a job canceled with `cancel` in the queue]
- `ipps://` printers bring self-signed certificates. Etiketto does not
  check them at all (CUPS remembers a printer's certificate the first time
  and warns if it changes); the risk is someone in the same network
  pretending to be the printer and reading the label. [confirmed]
- Checked with `ippeveprinter` (CUPS's simulated IPP Everywhere printer),
  directly and through a CUPS queue. An A4 label arrives as a
  595.276 × 841.89 pt PDF page, directly and through CUPS (2480 × 3508 px
  at 300 dpi inside). An HP LaserJet Pro 4002 accepted the same
  job with `Validate-Job` (nothing printed). The PWG Raster and Apple
  Raster pages convert with CUPS's own `pwgtopdf` (libcupsfilters). An
  `ippeveprinter -f image/urf` (no PDF, no PWG Raster: before, "no
  format") takes the A4 label as Apple Raster, W8 at 600 dpi, and stores
  a file that decodes to it. [confirmed]

## Apple Raster (URF)

AirPrint's page format, `image/urf`, for printers that list neither PDF
nor PWG Raster. Apple has not published it; the open source
implementations define it: OpenPrinting libcups (`cups/raster-stream.c`,
writer and reader; `tools/ippeveprinter.c`), PAPPL
(`pappl/printer-driver.c`, `printer-infra.c`) and libcupsfilters
(`cupsfilters/raster.c`), all Apache-2.0, Copyright OpenPrinting, Apple
Inc., Easy Software Products, Michael R Sweet and Till Kamppeter.
`etiketto/backends/urf.py` follows their byte layout; it is written anew,
no code is copied. [spec: from these sources]

- File: `UNIRAST\0`, then the number of pages (32 bit, big endian).
- Per page a 32-byte header, big endian: bits per pixel (8 for `W8`, 24
  for `SRGB24`), colour space (0 sGray, 1 sRGB), duplex (1, one-sided),
  print quality (4, normal), media type and media source (0, auto: the
  printer uses what is loaded, the size in `media-col` says which), 6
  reserved bytes, width and height in pixels, the resolution in dpi (one
  value, square pixels), 8 reserved bytes. Auto is always valid:
  libcups's `apple_media_types[0]` in `cups/raster-stream.c` is "auto",
  and PAPPL (`pappl/printer-driver.c`) notes that auto (0) is always
  present, also when `IS`/`MT` do not list it. [spec: from these sources]
- The rows, top to bottom, compressed like PWG Raster's: a byte with the
  number of identical lines minus 1 (up to 256 lines), then runs of a
  repeated pixel (0x00–0x7f: the next pixel 1–128 times) and literal
  pixels (0x81–0xff: 2–128 pixels follow). A lone pixel is a run of one,
  as libcups writes it. 0x80 (the rest of the line white) is never
  written, the decoder takes it. sGray is 0 black, 255 white; there is no
  1-bit depth.
- `urf-supported` keywords Etiketto uses (also from one comma-joined
  value): `SRGB24` or `W8` (colour space; every AirPrint printer must
  take `W8`, so a printer that lists no colour space at all gets it),
  `RS<dpi>` and `RS<dpi>-<dpi>` (a list of resolutions, not a range),
  `CP<n>` (the printer makes copies). Not used: `V1.4`/`V1.5` (version),
  `DM` (duplex, nothing for labels), `PQ` (quality: always normal), `IS`
  and `MT` (media source and type: always auto), `OB`, `FN`, `IFU`,
  `OFU`, the `DEV…` and `ADOBERGB…` colour spaces. [spec] An HP LaserJet
  Pro 4002 lists `CP1, MT1-2-8-9-10-11, PQ3-4-5, RS600, SRGB24, OB10, W8,
  DEVW8, DEVRGB24, ADOBERGB24, DM3, IS1-1-2, V1.5, FN3` (it takes PDF, so
  it never gets URF). [confirmed: Get-Printer-Attributes]
- `urf.encode()` takes the pages as they come (a generator) and their
  number, like `pwg.encode_pages()`; `urf.join()` and `pwg.join()` put
  documents together (copies: the same one again).

## Label sheets

Several labels on one sheet of paper (A4, US Letter, others), placed as
the gLabels template database says (`etiketto/sheets`, see below).

- Each page is composed at 300 dpi (PWG Raster, Apple Raster, and PDF for
  a printer below 300 dpi such as 203: at the printer's resolution, the
  labels rendered at it too): the paper's size from the template,
  every label at its exact position (rounded to the pixel, 0.08 mm), round
  labels, ellipses and CDs clipped to their shape (rectangles are printed to
  their edges). Each position gets its own label image from a source
  (`label_at(n)` for the n-th label of the job), so a mail merge can give
  every position another row; printing copies is the source that always
  returns the same label.
- The positions to fill: the chosen ones on the first sheet (partly used
  sheets), every position on the sheets after it. Up to 999 labels a job.
- The pages go in `Print-Job`s of up to 10 pages each, every page once (no
  `copies`): a PDF with a page each, or PWG Raster or Apple Raster with
  the page count set; 23 sheets are jobs of 10, 10 and 3 pages. A job also
  stays within 64 MB: a raster page that would take it over goes in the
  next job; a PDF takes no further page once another as large as the one
  before would take it over (the first page always goes in). Pages are
  composed one at a time while the document is written: a job holds one
  page image at a time, plus the encoded pages of that job. A printer that only takes JPEG gets one job per page. The jobs
  take part in everything above: `Validate-Job` once before the first,
  a busy printer asked again with the same document, the jobs sent and not
  over canceled when a step fails; the message then counts sheets
  ("Sheets already printed: 10 of 23.", "The sheets still waiting in the
  queue were canceled."). Progress counts pages
  (`job-impressions-completed`). [spec] [confirmed with ippeveprinter:
  Avery L7160 with outlines, two pages, every label within a pixel of the
  template]
- Unprintable margins: `Get-Printer-Attributes` asks for
  `media-col-ready` (the paper loaded) and `media-col-database` (each with
  its `media-size` and `media-*-margin`, in hundredths of mm), and for the
  `media-*-margin-supported` lists. The entry for the sheet's paper size
  counts (loaded paper first); else the smallest margin the printer
  supports for any paper, without 0 (borderless, only when asked for).
  Borderless entries (all margins 0) are skipped. Etiketto never asks for
  other margins, it warns: if the content of a label (the label inset by
  the safety margin) reaches into the margin, the status line says which
  positions, and printing asks first. [spec] [confirmed with ippeveprinter:
  A4 3.4 mm left and right, 6.35 mm top and bottom]
- Outlines (for a test on plain paper): 0.2 mm grey lines just inside each
  label, the shape of the label (corner radius, ellipse, CD with hole).

### The template database

`etiketto/sheets/templates.json` is converted from gLabels' template files
(`templates/*-templates.xml`, MIT license, Copyright (C) 2001-2026 Jaye
Evins; license in `etiketto/sheets/LICENSE.glabels`) by
`tools/glabels_templates.py`: lengths in mm (also written with an
exponent, `3.93386e-16pt`), aliases (`equiv`) resolved, paper sizes from
`paper-sizes.xml`, 1750 sheets from 35 files. Some files use the XML
namespace `http://glabels.org/xmlns/3.0/`, most none: namespaces are
dropped when a file is read. The converter lists the templates and the
sheets kept per file, and stops at a file without any template (a change
in the format would otherwise drop sheets without a word). Left out (of
1797 templates, 654 of them aliases, in the version converted): 23 rolls
(`size="roll"`), 9 templates whose labels lie off the page (typing errors
in the database) and 1 with labels smaller than 5 mm; free-form labels
(`Label-path`) and templates with more than one kind of label would be
left out too (none at present). An alias goes with its template, and is
skipped if its template is missing or the part number is taken already
(14). gLabels gives a CD's hole as a radius; business-card CDs (a disc cut
off at the sides) keep the disc's diameter.

## Problems the printer reports

`printer-state-reasons` keywords; `-warning` and `-report` (toner low,
paper low) are no reason to stop, a keyword without suffix counts as an
error (RFC 8011 5.4.12). Keywords ending in `-low` (`toner-low`,
`media-low`, `marker-supply-low`) are warnings even without a suffix.
[spec]

| Keyword | Etiketto's message |
|---|---|
| `media-empty`, `media-needed` | out of paper (waits while printing) |
| `media-jam` | paper jammed (waits while printing) |
| `door-open`, `cover-open`, `interlock-open` | cover open (waits while printing) |
| `toner-empty`, `marker-supply-empty` | out of toner or ink |
| `paused`, `moving-to-paused`, printer state "stopped" | paused (a queue on this computer: resume it in the system settings) |
| `offline`, `timed-out` | printer does not answer |
| `connecting-to-device`, `sending-to-device`, `cups-…` | routine, ignored |
| anything else | generic message with the keyword in the details |

## Not done yet

- The unprintable margin the printer reports only warns on label sheets;
  it does not set the safety margin of a single label (the documentation
  suggests 5 mm).
- Label sheets: the margins are matched to the paper size as it is, not
  to a page the printer turns to portrait; a mail merge (a different label
  at every position) is prepared in the page composer, not in the
  interface yet.
- Sheet templates of gLabels with labels of several kinds on one sheet, and
  free-form labels, are not supported.
