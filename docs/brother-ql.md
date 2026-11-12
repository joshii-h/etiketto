# Brother QL: raster commands over the network and USB

How Tavletta talks to Brother QL label printers (QL-500 to QL-1115NWB,
including the red and black QL-800, QL-810W and QL-820NWB). **Not yet
tried on a printer**: no QL printer was available while this was written. The sources are Brother's public
"Raster Command Reference" documents (QL-500/550/560/570/580N/650TD/700/
1050/1060N, QL-710W/720NW, QL-800/810W/820NWB, QL-1100/1110NWB/1115NWB)
and the source code of [brother_ql](https://github.com/pklaus/brother_ql)
by Philipp Klaus and its fork
[brother-ql-next](https://github.com/LunarEclipse363/brother_ql) by
LunarEclipse (both GPL-3.0). Tavletta's code is written anew; no code is
copied.

Markers: **[ref]** in Brother's command references, **[spec]** in
brother_ql's source, **[assumed]** likely but in neither and not tried on
a printer, **[open]** unknown.

## Two backends, one printer

Tavletta designs continuous tape and die-cut labels differently, so the
QL appears twice in the list of printer models; both share
`tavletta/backends/brother_ql.py`:

- `brother_ql`, "Brother QL, continuous rolls": DK continuous rolls of 12,
  18, 29, 38, 50, 54, 62, 90, 102 and 104 mm; the label grows with its
  content or has a fixed length, like on the VC-500W.
- `brother_ql_diecut`, "Brother QL, die-cut labels": DK die-cut labels in
  the sizes of the table below, content across or along the roll, like on
  cab printers.

Both entries share one setup (`settings.json` under `printers.brother_ql`:
host, cut, colours, quality), and what the network search learned about a
printer. When the printer reports a roll of the other kind, the status
row says the loaded roll does not match (error code `wrong_media`) and a
notice offers to switch to the other entry.

## Transport

- TCP port 9100 and USB take the same bytes. [ref] [spec]
- USB: the kernel's printer class driver (usblp) makes the printer
  `/dev/usb/lpN`; the status can be read through it. [spec] Tavletta
  finds QL printers by the IEEE 1284 device ID in sysfs (`MFG:Brother`,
  `MDL:QL-…`) and addresses them like CUPS:
  `usb://Brother/QL-820NWB?serial=...`. [spec: LPrint shows
  `MFG:Brother;CMD:PT-CBP;MDL:QL-820NWB;`]
- Models with "Editor Lite" (QL-700, QL-800 series, green LED) show up as
  a USB drive while it is on; hold its button until the LED goes out.
  [spec: fork's README]
- Network search: mDNS services `_pdl-datastream._tcp` and
  `_printer._tcp` whose name contains a QL model ("Brother QL-820NWB").
  [assumed: the names Brother's network printers announce]
- On TCP a job is sent 16 kB at a time; the 10 s timeout restarts with
  every piece the printer takes, so a printer whose buffer is full for a
  while does not fail a long job (on USB, `usb.Connection` does the same).
  [assumed: the printer stops reading while its buffer is full]
- brother_ql does not read the status over the network. Tavletta asks
  anyway and waits 3 s; a printer that does not answer is shown as
  connected without a status, and jobs are sent without checking the
  loaded roll. [ref: the references describe the status for USB, serial
  and network alike] [open: which models answer on port 9100]

## Status

Request: NUL bytes (400) to clear a half-received command, then `ESC i S`
(`1B 69 53`). No `ESC @` in a status query: it would end a print that
is running. The answer is 32 bytes. [ref]

| Byte | Meaning | Used for |
|---|---|---|
| 0-2 | `80 20 42` | recognising a packet; bytes before it are dropped (at most 4 kB) |
| 3 | series code: `30` or `34` | - |
| 4 | model code | the model, if neither USB nor the network search named it |
| 8, 9 | error information 1 and 2 (bits) | errors |
| 10 | media width in mm | the loaded roll |
| 11 | media type: continuous `0A` (old series, QL-1100 series) or `4A` (QL-710W, QL-720NW, QL-800 series); die-cut `0B` or `4B`; `00` none | the loaded roll |
| 17 | media length in mm, 0 for continuous | the loaded roll |
| 18 | status type: `00` reply, `01` printing completed, `02` error, `04` turned off, `05` notification, `06` phase change | progress, errors |
| 19 | phase: `00` waiting to receive, `01` printing | busy, done |
| 25 | reserved in Brother's references; `81` with the red and black roll on a QL-8xx [assumed: reported by other projects (sklad, thermal-label docs)] | preselects Red and black |

[ref]

Model codes (byte 4): QL-500 and QL-550 `4F`, QL-560 `31`, QL-570 `32`,
QL-580N `33`, QL-650TD `51`, QL-700 `35`, QL-710W `36`, QL-720NW `37`,
QL-800 `38`, QL-810W `39`, QL-820NWB `41`, QL-1050 `50`, QL-1060N `34`,
QL-1100 `43`, QL-1110NWB `44`, QL-1115NWB `45`. [ref] `4F` is taken as
the QL-500, which has no cutter (no cut commands, no cut row), unless the
USB device ID or the network name says QL-550. The QL-600 and QL-1100NWB
are known only from the fork's model list.

Errors and what Tavletta makes of them [ref for the bits, the mapping is
Tavletta's]:

| Bit | Meaning | Tavletta error |
|---|---|---|
| 1.0, 1.1 | no media, end of media (die-cut) | out of labels |
| 1.2 | cutter jam | label jammed |
| 1.4 | printer in use | busy |
| 1.5, 1.6, 1.7 | printer turned off, high-voltage adapter, fan | generic |
| 2.0 | replace media (other media than the job says) | the loaded roll does not match |
| 2.1, 2.2, 2.7 | expansion buffer full, communication error, system error | generic |
| 2.3 | communication buffer full | busy |
| 2.4 | cover open | cover open |
| 2.6 | media cannot be fed, also the end of a continuous roll | continuous roll: out of paper; die-cut: label jammed |

Status type `04` (turned off) is reported as a generic error.

- The loaded roll is found by (type, width, length) in the media table
  below. The references disagree on two lengths: 60 × 86 mm is reported
  as 86 (QL-800 series) or 87 (QL-1100 series), 102 × 152 mm as 152
  (QL-1100 series) or 153 (brother_ql); both are accepted, and the print
  information repeats what the printer reported. [ref] [spec]
- A 62 mm roll can be the black DK-22205 or the red and black DK-22251;
  the status does not tell them apart. [ref: bytes 23-31 are reserved]
- For die-cut labels the status row shows the loaded size, and the print
  dialog warns if it is not the label's (width or height more than 1 mm
  off) or the distance (length + 3 mm) differs. [assumed: the 3 mm gap of
  DK die-cut labels]

## Models

Numbers in dots at 300 dpi. [ref; QL-600 and QL-1100NWB from brother_ql's
fork]

| Model | Length (dots) | Pins | `ESC i a` | Cutter | `ESC i K` | PackBits | 600 dpi | Two colours | NULs |
|---|---|---|---|---|---|---|---|---|---|
| QL-500 | 295-11811 | 720 | no | no | no | no | no | no | 200 |
| QL-550, QL-560 | 295-11811 | 720 | no | yes | yes | no | no | no | 200 |
| QL-570, QL-700 | 150-11811 | 720 | no | yes | yes | no | yes | no | 200 |
| QL-580N | 150-11811 | 720 | yes | yes | yes | yes | yes | no | 200 |
| QL-600 | 150-11811 | 720 | yes | yes | yes | yes | no [assumed] | no | 200 |
| QL-650TD | 295-11811 | 720 | yes | yes | yes | no (serial port only) | no | no | 200 |
| QL-710W, QL-720NW | 150-11811 | 720 | yes | yes | yes | yes | yes | no | 200 |
| QL-800 | 150-11811 | 720 | yes | yes | yes | no | yes | yes | 400 |
| QL-810W, QL-820NWB | 150-11811 | 720 | yes | yes | yes | yes | yes | yes | 400 |
| QL-1050 | 295-35433 | 1296 | yes | yes | yes | yes | no | no | 350 |
| QL-1060N | 295-35433 | 1296 | yes | yes | yes | yes | no | no | 200 |
| QL-1100 series | 301-35434 | 1296 | yes | yes | yes | yes | yes | no | 350 |

- The model comes from the USB device ID, else from the name the network
  search found, else from the model code in the status.
- An unknown model gets what every model takes: no PackBits, no 600 dpi,
  400 NULs, at least 301 lines; 1296 pins if a roll wider than 62 mm is
  loaded. [assumed: `ESC i a` and `ESC i K` are ignored by models that do
  not know them]

## Media

Printable dots across and along the feed at 300 dpi, the right margin in
pins on the 720 and the 1296 pin head, and the feed margin of `ESC i d`.
[ref; the 18 mm roll only in brother_ql's fork]

| Roll | Status (mm) | Printable | Right, 720 | Right, 1296 | Feed margin |
|---|---|---|---|---|---|
| 12 mm | 12 × 0 | 106 | 29 | 74 | 35 |
| 18 mm | 18 × 0 | 234 | 171 | 215 [assumed] | 14 |
| 29 mm | 29 × 0 | 306 | 6 | 50 | 35 |
| 38 mm | 38 × 0 | 413 | 12 | 56 | 35 |
| 50 mm | 50 × 0 | 554 | 12 | 56 | 35 |
| 54 mm | 54 × 0 | 590 | 0 | 44 | 35 |
| 62 mm (also red and black) | 62 × 0 | 696 | 12 | 56 | 35 |
| 90 mm (QL-1100 series) | 90 × 0 | 1027 [assumed] | - | 56 [assumed] | 35 |
| 102 mm (4 inch models) | 102 × 0 | 1164 | - | 56 | 35 |
| 103 mm (QL-1100, QL-1110NWB) | 104 × 0 | 1200 | - | 38 | 35 |
| 62 × 29 | 62 × 29 | 696 × 271 | 12 | 56 | 0 |
| 29 × 90 | 29 × 90 | 306 × 991 | 6 | 50 | 0 |
| 62 × 100 | 62 × 100 | 696 × 1109 | 12 | 56 | 0 |
| 17 × 54 | 17 × 54 | 165 × 566 | 0 | 44 | 0 |
| 17 × 87 | 17 × 87 | 165 × 956 | 0 | 44 | 0 |
| 23 × 23 | 23 × 23 | 236 × 202 | 42 | 85 | 0 |
| 29 × 42 | 29 × 42 | 306 × 425 | 6 | 50 | 0 |
| 38 × 90 | 38 × 90 | 413 × 991 | 12 | 56 | 0 |
| 39 × 48 | 39 × 48 | 425 × 495 | 6 | 50 | 0 |
| 52 × 29 | 52 × 29 | 578 × 271 | 0 | 44 | 0 |
| 54 × 29 (QL-800 series) | 54 × 29 | 602 × 271 | 59 | - | 0 |
| 60 × 86 | 60 × 86 or 87 | 672 × 954 | 24 | 68 | 0 |
| 102 × 51 (4 inch models) | 102 × 51 | 1164 × 526 | - | 56 | 0 |
| 102 × 152 (4 inch models) | 102 × 152 or 153 | 1164 × 1660 | - | 56 | 0 |
| 103 × 164 (QL-1100, QL-1110NWB) | 104 × 164 | 1200 × 1822 | - | 38 | 0 |
| round 12, 24, 58 mm | 12 × 12, 24 × 24, 58 × 58 | 94, 236, 618 square | 113, 42, 51 | 156, 85, 94 | see below |

- The references give 1050 and 1100 tables that differ by a pin in places
  (23 × 23: 84 or 85) and contain sums that do not add up to 1296 (62 mm
  on the QL-1100: 544 + 696 + 44); Tavletta takes the values that add up.
- Feed margin of 12 mm round labels: 35 dots on the QL-500, QL-550,
  QL-560, QL-570, QL-580N and QL-700, 0 on the others. [ref]
- The 90 mm continuous roll appears only in the QL-1100 status table,
  without a print area: Tavletta assumes 1.5 mm unprintable on both sides
  like the other rolls. [assumed]
- Brother restricts some wide rolls to some models (the QL-1115NWB takes
  no 103 mm); Tavletta does not, the printer holds only what fits.

## A print job

```
1B 69 61 01         ESC i a 1: raster mode (only models that know it)
00 × 200/350/400    invalidate
1B 40               ESC @: initialise
1B 69 61 01         ESC i a 1 again
1B 69 53            ESC i S: status (the answer is not waited for)
-- each copy is a page --
1B 69 7A n1..n10    print information
1B 69 4D n          ESC i M: bit 6 auto cut (models with a cutter)
1B 69 41 01         ESC i A 1: cut every label (only with auto cut)
1B 69 4B n          ESC i K: bit 0 two colours, bit 3 cut at end, bit 6 600 dpi
1B 69 64 lo hi      ESC i d: feed margin in dots
4D 02               M 2: PackBits (only models that take it)
lines               67 00 n data, or 77 01 n black + 77 02 n red per line
0C or 1A            0C: more pages follow, 1A: the last page
```

[ref] [spec]

- Print information: n1 = `8E` (media type, width and length valid,
  printer recovery) plus `40` (quality first) except for two colours,
  where Brother says it is invalid; n2 = `0A`/`0B` (also on models that
  report `4A`/`4B`); n3 width in mm, n4 length in mm (0 for continuous; as
  reported by the status if it did), n5-n8 number of lines (32 bit,
  little endian; one line of two colours counts once), n9 = 0 for the
  first page and 1 for the following ones, n10 = 0. [ref]
- Byte for byte, a 62 mm continuous label of 20 empty lines on a QL-720NW
  without compression is the example in brother_ql's notes;
  `tests/test_brother_ql.py` checks it and one job per model class.
- Copies are pages of one job, `0C` between them; the printer reports each
  page. [ref]
- Cut: "Full cut" sets auto cut, cut every label and cut at end; "Cut
  after the last label" only cut at end, the copies stay together; "No
  cut" none of them. The QL-500 gets none of these commands and the print
  dialog shows no cut row for it. A half cut does not exist on the QL.
  [ref for the bits] [assumed: cut at end without auto cut cuts once
  after the last page]
- High resolution: the label is rendered at 600 dpi and halved across the
  head; each line covers 1/600 inch along the feed and the line count
  doubles. [ref] [assumed: limits in lines double too, the feed margin
  stays in 300 dpi dots]
- Two colours: only on the QL-800 series with a 62 mm continuous roll, at
  300 dpi. Red-ish pixels (hue below 40 or above 210 of 255, saturation
  above 100, value above 80) go to the red layer, the rest prints black
  where it is darker than mid-grey. [spec for the split]

### The red and black roll

On DK-22251 every job has to be a two-colour job, even one with black
content only [spec: brother_ql's command line help], and a two-colour
job on a plain roll fails [assumed]. Brother's status does not tell the
rolls apart, so Tavletta:

- takes the roll it remembers for this printer, else what the print
  dialog says (Colours: Black, or Red and black). If bit 7 of status byte
  25 is set, the dialog lists Red and black first, so it is preselected
  [assumed, see above]; a wrong hint cannot lock the choice;
- if the printer refuses the job with "replace media" before printing,
  sends it once more as the other kind on the same connection and says so
  ("Printed with Red and black, as the loaded roll needs it") [assumed:
  the refusal comes before anything is printed, and the printer takes the
  next job without a key press]. Every page of the refused job is
  refused: for the retry only, everything that arrives before the answer
  to its own `ESC i S` is skipped, so those notices are not taken for the
  retry's. If that answer never comes after packets were skipped, one
  more `ESC i S` decides: an error is raised, otherwise the job counts as
  sent;
- remembers the roll once the printer reported a label printed; from then
  on the job follows the roll whatever the dialog says, and the dialog
  offers only what the roll prints, until a job is refused again (the
  roll was changed). If the retry is refused too, the problem is
  something else: the error is shown and the remembered roll is
  forgotten.

## Raster

- Each line runs across the print head. Continuous labels are turned by
  90 degrees (counter-clockwise, as in brother_ql's notes); die-cut labels
  come from the interface already in printer orientation, content along
  the roll turned the same way. [spec]
- 1 bit by a threshold at mid-grey, no dithering: codes and text keep
  sharp edges. Photos print as hard black and white; the preview shows
  exactly that. [spec: brother_ql's default is a threshold too]
- The image is placed the right margin's pins from the right end of the
  head (720 or 1296 pins), then mirrored: the first byte of a line holds
  the rightmost pins, bit 7 first. [ref]
- PackBits per line where the model takes it; runs of at least 3 bytes,
  so a line grows by at most one byte per 128 and fits the one-byte
  length. [ref for the command and the one-byte length]
- Continuous labels shorter than the model's minimum get white at both
  ends; longer than its maximum are refused (1 m on the narrow models).
  [ref for the limits]
- Die-cut labels are rendered at their full size; the unprintable rim
  (1.5 mm across, about 3 mm along the feed) is cut off, the printable
  area taken from the middle. [ref for the rim] [assumed: the 6 mm rim of
  102 × 152 mm is also taken from the middle]
- Another width loaded ("Print anyway"): the label is scaled to the
  loaded roll, like the VC-500W does. Another die-cut size: the label is
  scaled to fit and centred.

## After sending

The printer reports each page: phase change to printing, printing
completed, and after the last page a phase change to waiting. [ref]
Tavletta counts the "printing completed" packets for the progress; an
error packet stops the job with the number of labels printed so far.

- A printer that sends no notice within 10 s (answers to `ESC i S` do not
  count) is taken as one that does not report on this connection: the job
  was sent. [spec: brother_ql waits up to 10 s] [assumed: the same on TCP]
- If the notices stop after the first one (60 s without one), Tavletta
  asks `ESC i S` once: waiting to receive without an error means the job
  is done, anything else a timeout with the labels printed so far.
- While another job prints (phase 1), Tavletta asks `ESC i S` every 0.5 s,
  without NULs, for up to 180 s. [assumed]

## To try on a printer

No QL printer was at hand; these need one:

- Does a QL-8xx take a new job on the same connection right after
  refusing one with "replace media", or does it wait for a key press
  (and does anything come out of the refused job)?
- What does status byte 25 report with DK-22251, with DK-22205 and with
  other rolls, on the QL-800, QL-810W and QL-820NWB?
- Do models answer `ESC i S` on port 9100, and do they send the page
  notices there?
- Several copies as pages of one job, and "Cut after the last label"
  (cut at end without auto cut).
- 600 dpi: line limits and feed margin; the right margin of 60 × 86 mm
  and 54 × 29 mm; the 90 mm roll's print area.

## Not used yet

- Automatic status notification (`ESC i !`), P-touch Editor templates,
  `ESC i U` settings, auto power off.
- Zero raster lines (`Z`) with compression.
- Dithering for photos as a choice.
- The P-touch PT models brother_ql also drives (`G` lines with a 16-bit
  length): a separate backend.
