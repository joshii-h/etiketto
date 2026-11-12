# Brother QL: raster commands over the network and USB

How Etiketto talks to Brother QL label printers (QL-500 to QL-1115NWB,
including the red and black QL-800, QL-810W and QL-820NWB). No QL printer
was available while this was written: everything here comes from the
source code of [brother_ql](https://github.com/pklaus/brother_ql) by
Philipp Klaus and its fork
[brother-ql-next](https://github.com/LunarEclipse363/brother_ql) by
LunarEclipse (both GPL-3.0), which are based on Brother's "Raster Command
Reference" documents. Etiketto's code is written anew from notes on them;
no code is copied.

Markers: **[spec]** in brother_ql's source, **[assumed]** likely but not
in that source and not tried on a printer, **[open]** unknown.

## Two backends

Etiketto designs continuous tape and die-cut labels differently, so the
QL appears twice in the list of printer models; both share
`etiketto/backends/brother_ql.py`:

- `brother_ql`, "Brother QL, continuous rolls": DK continuous rolls of 12,
  18, 29, 38, 50, 54, 62, 102 and 104 mm, the label grows with its content
  or has a fixed length, like on the VC-500W.
- `brother_ql_diecut`, "Brother QL, die-cut labels": DK die-cut labels in
  the sizes of the table below, content across or along the roll, like on
  cab printers.

A roll of the other kind in the printer is reported as "the loaded roll
does not match the label" (error code `wrong_media`).

## Transport

- TCP port 9100, the same bytes as on USB. [spec]
- USB: the kernel's printer class driver (usblp) makes the printer
  `/dev/usb/lpN`; reading the status works through it. [spec] Etiketto
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
- brother_ql does not read the status over the network. Etiketto asks
  anyway and waits 3 s for the answer; a printer that does not answer
  is shown as connected without a status, and jobs are sent without the
  check of the loaded roll. [assumed: newer models answer on port 9100]

## Status

Request `ESC i S` (`1B 69 53`); the answer is 32 bytes, starting with
`80 20 42`. [spec] Etiketto sends 400 NUL bytes and `ESC @` first, which
clears a job left half received by an earlier session. [spec: the NULs
"invalidate" the command buffer]

| Byte | Meaning | Used for |
|---|---|---|
| 0-2 | `80 20 42` | recognising a packet; bytes before it are dropped (at most 4 kB) |
| 4 | model code | the model, if neither USB nor the network search named it [assumed: table below] |
| 8, 9 | error information 1 and 2 (bits) | errors |
| 10 | media width in mm | the loaded roll |
| 11 | media type: `0A` continuous, `0B` die-cut, `00` none | the loaded roll |
| 17 | media length in mm, 0 for continuous | the loaded roll |
| 18 | status type: `00` reply, `01` printing completed, `02` error, `05` notification, `06` phase change | progress |
| 19 | phase: `00` waiting to receive, `01` printing | busy |

[spec: brother_ql's reader]

Errors and what Etiketto makes of them [spec for the bits, the mapping is
Etiketto's]:

| Bit | Meaning | Etiketto error |
|---|---|---|
| 1.0, 1.1 | no media, end of media | out of labels |
| 1.2 | cutter jam | label jammed |
| 1.4 | main unit in use | busy |
| 1.5, 1.6, 1.7 | printer turned off, high-voltage adapter, fan | generic |
| 2.0 | replace media (other media than the job says) | the loaded roll does not match |
| 2.1, 2.2, 2.7 | expansion buffer full, communication error, system error | generic |
| 2.3 | communication buffer full | busy |
| 2.4 | cover open | cover open |
| 2.6 | media cannot be fed | label jammed |

- The loaded roll is found by (type, width, length) in the media table
  below. A 62 mm roll can be the black DK-22205 or the red and black
  DK-22251; the status does not tell them apart (byte 24, "tape colour",
  is not used by brother_ql). Etiketto offers red and black on two-colour
  models whenever a 62 mm continuous roll is loaded. [open]
- For die-cut labels Etiketto reports a label every length + 3 mm, so that
  the interface warns about another label size. [assumed: the gap of DK
  die-cut labels]

## Models

Numbers in dots at 300 dpi. "Old" models know neither `ESC i a` nor
compression. [spec: models.py of both repositories; QL-600 and QL-1100NWB
only in the fork]

| Model | Length (dots) | Pins | Extra right offset | `ESC i a` | Cutter | `ESC i K` | PackBits | Two colours | NULs |
|---|---|---|---|---|---|---|---|---|---|
| QL-500 | 295-11811 | 720 | 0 | no | no | no | no | no | 200 |
| QL-550, QL-560 | 295-11811 | 720 | 0 | no | yes | yes | no | no | 200 |
| QL-570, QL-700 | 150-11811 | 720 | 0 | no | yes | yes | no | no | 200 |
| QL-580N, QL-600, QL-710W, QL-720NW | 150-11811 | 720 | 0 | yes | yes | yes | yes | no | 200 |
| QL-650TD | 295-11811 | 720 | 0 | yes | yes | yes | yes | no | 200 |
| QL-800 | 150-11811 | 720 | 0 | yes | yes | yes | no | yes | 400 |
| QL-810W, QL-820NWB | 150-11811 | 720 | 0 | yes | yes | yes | yes | yes | 400 |
| QL-1050, QL-1060N | 295-35433 | 1296 | 44 | yes | yes | yes | yes | no | 200 |
| QL-1100, QL-1100NWB, QL-1110NWB, QL-1115NWB | 301-35434 | 1296 | 44 | yes | yes | yes | yes | no | 200 |

- The model comes from the USB device ID, else from the name the network
  search found, else from the model code in the status. [spec for the
  first]
- Model codes (status byte 4): QL-500 `4F`, QL-560 `31`, QL-570 `32`,
  QL-580N `33`, QL-650TD `51`, QL-700 `35`, QL-710W `36`, QL-720NW `37`,
  QL-800 `38`, QL-810W `39`, QL-820NWB `41`, QL-1050 `50`, QL-1060N `34`,
  QL-1100 `43`, QL-1110NWB `44`, QL-1115NWB `45`. [assumed: from memory of
  Brother's references, not in brother_ql]
- An unknown model gets what every model takes: no PackBits, no 600 dpi,
  400 NULs, at least 301 lines; 1296 pins if a roll wider than 62 mm is
  loaded. [assumed: `ESC i a` and `ESC i K` are ignored by models that do
  not know them]
- 600 dpi along the feed on every model with `ESC i K`. [spec: the fork's
  `dpi_600`] [open: whether the QL-550 and QL-560 really print it]

## Media

Printable dots across and along the feed at 300 dpi, the right offset in
dots and the feed margin of `ESC i d`. [spec: labels.py of the fork; the
18 mm roll and 54 × 29 mm only there]

| Roll | Status (mm) | Printable | Offset | Feed margin |
|---|---|---|---|---|
| 12 mm | 12 × 0 | 106 | 29 | 35 |
| 18 mm | 18 × 0 | 234 | 171 | 14 |
| 29 mm | 29 × 0 | 306 | 6 | 35 |
| 38 mm | 38 × 0 | 413 | 12 | 35 |
| 50 mm | 50 × 0 | 554 | 12 | 35 |
| 54 mm | 54 × 0 | 590 | 0 | 35 |
| 62 mm (also red and black) | 62 × 0 | 696 | 12 | 35 |
| 102 mm (4 inch models) | 102 × 0 | 1164 | 12 | 35 |
| 104 mm (QL-1100 series) | 104 × 0 | 1200 | 12 | 35 |
| 62 × 29 | 62 × 29 | 696 × 271 | 12 | 0 |
| 29 × 90 | 29 × 90 | 306 × 991 | 6 | 0 |
| 62 × 100 | 62 × 100 | 696 × 1109 | 12 | 0 |
| 17 × 54 | 17 × 54 | 165 × 566 | 0 | 0 |
| 17 × 87 | 17 × 87 | 165 × 956 | 0 | 0 |
| 23 × 23 | 23 × 23 | 202 × 202 | 42 | 0 |
| 29 × 42 | 29 × 42 | 306 × 425 | 6 | 0 |
| 38 × 90 | 38 × 90 | 413 × 991 | 12 | 0 |
| 39 × 48 | 39 × 48 | 425 × 495 | 6 | 0 |
| 52 × 29 | 52 × 29 | 578 × 271 | 0 | 0 |
| 54 × 29 | 54 × 29 | 598 × 271 | 60 | 0 |
| 60 × 87 | 60 × 87 | 672 × 954 | 18 | 0 |
| 102 × 51, 102 × 153 (4 inch models) | as named | 1164 × 526, 1164 × 1660 | 12 | 0 |
| 104 × 164 (QL-1100 series) | 104 × 164 | 1200 × 1822 | 12 | 0 |
| round 12, 24, 58 mm | 12 × 12, 24 × 24, 58 × 58 | 94, 236, 618 square | 113, 42, 51 | 35, 0, 0 |

- The extra right offset of the 4 inch models (44 dots) is added to the
  roll's. [spec]
- Round labels report themselves as die-cut (`0B`) with their diameter as
  width and length. [assumed]
- Brother restricts some wide rolls to some 4 inch models; Etiketto does
  not, the printer holds only what fits.

## A print job

```
1B 69 61 01         ESC i a 1: raster mode (only models that know it)
00 × 200 or 400     invalidate
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

[spec: raster.py and conversion.py]

- Print information: n1 = `CE` (media type, width and length valid,
  quality first), n2 = `0A`/`0B`, n3 width in mm, n4 length in mm (0 for
  continuous), n5-n8 number of lines (32 bit, little endian), n9 = 0 for
  the first page and 1 for the following ones, n10 = 0. [spec]
- Byte for byte, a 62 mm continuous label of 20 empty lines on a QL-720NW
  without compression is the example in brother_ql's notes;
  `tests/test_brother_ql.py` checks it and one job per model class.
- Copies are pages of one job, `0C` between them. [spec for the bytes]
  [assumed: brother_ql never sends more than one page per job]
- Cut: "Full cut" sets auto cut, cut every label and cut at end; "Cut
  after the last label" only cut at end, the copies stay together; "No
  cut" none of them. Models without a cutter get none of these commands.
  A half cut does not exist on the QL. [spec for the bits] [assumed: cut at
  end without auto cut cuts once after the last page]
- High resolution: the label is rendered at 600 dpi and halved across the
  head; each line covers 1/600 inch along the feed and the line count
  doubles. [spec: the fork's `dpi_600`] [assumed: limits in lines double
  too, the feed margin stays in 300 dpi dots]
- Two colours: only on QL-8xx with a 62 mm continuous roll, at 300 dpi.
  Red-ish pixels (hue below 40 or above 210 of 255, saturation above 100,
  value above 80) go to the red layer, the rest prints black where it is
  darker than mid-grey. [spec for the split] [assumed: no 600 dpi in two
  colours]

## Raster

- Each line runs across the print head. Continuous labels are turned by
  90 degrees (counter-clockwise, as in brother_ql's notes); die-cut labels
  come from the interface already in printer orientation, content along
  the roll turned the same way. [spec]
- 1 bit by a threshold at mid-grey, no dithering: codes and text keep
  sharp edges. Photos print as hard black and white; the preview shows
  exactly that. [spec: brother_ql's default is a threshold too]
- The image is placed `offset` dots from the right end of the head
  (720 or 1296 pins), then mirrored: the first byte of a line holds the
  rightmost pins, bit 7 first. [spec]
- PackBits per line where the model takes it; runs of at least 3 bytes,
  so a line grows by at most one byte per 128 and fits the one-byte
  length. [spec for the command and the one-byte length]
- Continuous labels shorter than the model's minimum get white at both
  ends; longer than its maximum are refused (1 m on the narrow models).
  [spec for the limits]
- Die-cut labels are rendered at their full size; the unprintable rim
  (about 1.5 mm across, 3 mm along the feed) is cut off, the printable
  area taken from the middle. [assumed: the printable area is centred]
- Another width loaded ("Print anyway"): the label is scaled to the
  loaded roll, like the VC-500W does. Another die-cut size: the label is
  scaled to fit and centred.

## After sending

The printer reports each page: phase change to printing, printing
completed, and after the last page a phase change to waiting. [spec:
helpers.send waits for these] Etiketto counts the "printing completed"
packets for the progress; an error packet stops the job with the number
of labels printed so far. A printer that sends no notice within 10 s
(answers to `ESC i S` do not count) is taken as one that does not report
on this connection: the job was sent. [spec: brother_ql waits up to 10 s]
[assumed: the same on TCP]

While another job prints (phase 1), Etiketto asks `ESC i S` every 0.5 s,
without NULs, for up to 180 s. [assumed]

## Not used yet

- P-touch Editor templates, `ESC i U` settings, auto power off.
- Byte 24 (tape colour) to tell DK-22251 from DK-22205. [open]
- Dithering for photos as a choice.
- The P-touch PT models brother_ql also drives (`G` lines with a 16-bit
  length): a separate backend.
