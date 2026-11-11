# System printer: IPP to CUPS or to the printer

How Etiketto prints on printers the operating system knows. Everything goes
through IPP (Internet Printing Protocol, RFC 8010 and RFC 8011), the
protocol CUPS itself speaks and that every IPP Everywhere / AirPrint printer
understands. Python's standard library is enough; there is no dependency on
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
  formats, `print-scaling-supported` and `media-size-supported`. A printer
  that reports a problem gets no job. [spec]
- The page size comes from the label in millimetres (A4 is exactly 21000 ×
  29700 hundredths of mm). If the printer does not list it, the page is
  turned to portrait when that size is listed; otherwise Etiketto says so
  before sending anything, instead of the printer silently using its
  default paper (it would answer 0x0001, "attributes ignored"). [spec]
  [confirmed with an HP LaserJet Pro 4002: 50 × 30 mm is below its range]
- `Print-Job` with one page in the label's exact size: PDF if the printer
  or CUPS takes it (Pillow's JPEG inside at quality 95 without chroma
  subsampling, so that small text stays sharp), else PWG Raster, which
  every IPP Everywhere printer must take (8-bit sRGB or sGray, at 300 dpi
  or the next resolution the printer lists), else JPEG. `media-col`
  carries the size, `print-scaling=none` (if supported) keeps it at 100 %,
  `copies` is the number of labels, `requesting-user-name` the login
  name. [spec]
- `Get-Job-Attributes` every second; the job's own state decides:
  completed is done, canceled or aborted is an error, processing-stopped
  cancels the job with the printer's reason. While a job prints, CUPS
  reports `connecting-to-device` and `cups-waiting-for-job-completed`
  without a suffix: routine, not a problem. A job printing for more than
  5 minutes is canceled; one waiting behind other jobs is left to print
  when its turn comes. A few missed answers are tolerated; a printer that
  forgot the job already (not found) counts as done. [spec] [confirmed
  through a CUPS queue to ippeveprinter]
- `ipps://` printers bring self-signed certificates. Etiketto does not
  check them at all (CUPS remembers a printer's certificate the first time
  and warns if it changes); the risk is someone in the same network
  pretending to be the printer and reading the label. [confirmed]
- Checked with `ippeveprinter` (CUPS's simulated IPP Everywhere printer),
  directly and through a CUPS queue: an A4 label arrives as a
  595.276 × 841.89 pt PDF page. An HP LaserJet Pro 4002 accepted the same
  job with `Validate-Job` (nothing printed). The PWG Raster pages convert
  with CUPS's own `pwgtopdf`. [confirmed]

## Problems the printer reports

`printer-state-reasons` keywords; `-warning` and `-report` (toner low,
paper low) are no reason to stop, a keyword without suffix counts as an
error (RFC 8011 5.4.12). [spec]

| Keyword | Etiketto's message |
|---|---|
| `media-empty`, `media-needed` | out of paper |
| `media-jam` | paper jammed |
| `door-open`, `cover-open`, `interlock-open` | cover open |
| `toner-empty`, `marker-supply-empty` | out of toner or ink |
| `paused`, `moving-to-paused`, printer state "stopped" | paused (a queue on this computer: resume it in the system settings) |
| `offline`, `timed-out` | printer does not answer |
| `connecting-to-device`, `sending-to-device`, `cups-…` | routine, ignored |
| anything else | generic message with the keyword in the details |

## Not done yet

- The unprintable margin the printer reports (`media-col-ready`) is not
  used for the safety margin yet; the documentation suggests 5 mm.
- Label sheets (several labels on one page) come with label sheet support.
