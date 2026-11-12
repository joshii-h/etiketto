# System printer: IPP to CUPS or to the printer

How Etiketto prints on printers the operating system knows. Everything goes
through IPP (Internet Printing Protocol, RFC 8010 and RFC 8011), the
protocol CUPS itself speaks and that every IPP Everywhere printer
understands. A printer that only takes AirPrint's `image/urf` gets no job
directly (no format Etiketto can make, see below); set up as a CUPS queue,
CUPS converts the PDF for it. Python's standard library is enough; there is no dependency on
CUPS libraries or drivers.

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
  `copies-supported` and `operations-supported`. A printer that reports a problem, or whose
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
- `Print-Job` with one page in the label's exact size: PDF if the printer
  or CUPS takes it (one JPEG per page at quality 95 without chroma
  subsampling, so that small text stays sharp; the page exactly the size in
  millimetres, A4 is 595.276 × 841.89 pt), else PWG Raster, which
  every IPP Everywhere printer must take (8-bit sRGB or sGray, at 300 dpi
  or the next resolution the printer lists), else JPEG. `media-col`
  carries the size, `print-scaling=none` (if supported) keeps it at 100 %,
  `copies` is the number of labels, `requesting-user-name` the login
  name. [spec]
- Copies: if the upper bound of `copies-supported` is below the number of
  labels, PWG Raster carries the page several times in one job
  (`TotalPageCount`), at most 10 pages per job (the document is made in
  memory: 10 noisy A4 pages are about 260 MB), so 23 labels are jobs of
  10, 10 and 3 pages; PDF and JPEG go as one job per label. The document
  is made once and sent again for every job of the same size. A printer
  that does not list `copies-supported` is trusted to make the copies.
  [spec]
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
  job with `Validate-Job` (nothing printed). The PWG Raster pages convert
  with CUPS's own `pwgtopdf`. [confirmed]

## Label sheets

Several labels on one sheet of paper (A4, US Letter, others), placed as
the gLabels template database says (`etiketto/sheets`, see below).

- Each page is composed at 300 dpi: the paper's size from the template,
  every label at its exact position (rounded to the pixel, 0.08 mm), round
  labels, ellipses and CDs clipped to their shape (rectangles are printed to
  their edges). Each position gets its own label image from a source
  (`label_at(n)` for the n-th label of the job), so a mail merge can give
  every position another row; printing copies is the source that always
  returns the same label.
- The positions to fill: the chosen ones on the first sheet (partly used
  sheets), every position on the sheets after it. Up to 999 labels a job.
- The pages go in `Print-Job`s of up to 10 pages each, every page once (no
  `copies`): a PDF with a page each, or PWG Raster with `TotalPageCount`
  set; 23 sheets are jobs of 10, 10 and 3 pages. Pages are composed one at
  a time while the document is written: a job holds one page image at a
  time, plus the encoded pages of that job (at most 10, as for copies
  above). A printer that only takes JPEG gets one job per page. The jobs
  take part in everything above: `Validate-Job` once before the first,
  a busy printer asked again with the same document, the jobs sent and not
  over canceled when a step fails; the message then counts sheets
  ("Sheets already printed: 10 of 23."). Progress counts pages
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
`tools/glabels_templates.py`: lengths in mm, aliases (`equiv`) resolved,
paper sizes from `paper-sizes.xml`. Left out: rolls, free-form labels
(`Label-path`), templates with more than one kind of label, labels smaller
than 5 mm, and a handful of templates whose labels lie off the page
(typing errors in the database). gLabels gives a CD's hole as a radius;
business-card CDs (a disc cut off at the sides) keep the disc's diameter.

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
