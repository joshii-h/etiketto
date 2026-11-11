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

The host field takes a printer URI (`ipp://…`, `ipps://…`), a CUPS queue
name (`HP_LaserJet`) or a host name / IP address (then
`ipp://<host>/ipp/print`).

## Printing

- `Get-Printer-Attributes` first: state, `printer-state-reasons`,
  `document-format-supported`, `print-scaling-supported`. A printer that
  reports a problem gets no job. [spec]
- `Print-Job` with one page in the label's exact size: PDF if the printer
  or CUPS takes it, else JPEG (mandatory for IPP Everywhere). `media-col`
  carries the size in hundredths of millimetres, `print-scaling=none` (if
  supported) keeps it at 100 %. `copies` is the number of labels. [spec]
- `Get-Job-Attributes` every second until the job is completed; a problem
  of the printer meanwhile, or no end after 5 minutes, cancels the job
  (`Cancel-Job`), so that it does not print later on its own. [spec]
- `ipps://` printers bring self-signed certificates; like CUPS, Etiketto
  does not check them. [confirmed]
- Checked with `ippeveprinter` (CUPS's simulated IPP Everywhere printer: a
  105 × 148 mm label arrives as a 297.6 × 419.52 pt PDF page) and an HP
  LaserJet Pro 4002, which accepted the same job with `Validate-Job`
  (nothing printed). [confirmed]

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
| `paused`, `moving-to-paused`, printer state "stopped" | paused |
| `offline`, `timed-out`, `connecting-to-device` | printer does not answer |
| anything else | generic message with the keyword in the details |

## Not done yet

- Printers that take neither PDF nor JPEG (PWG raster only).
- The unprintable margin the printer reports (`media-col-ready`) is not
  used for the safety margin yet.
- Label sheets (several labels on one page) come with label sheet support.
