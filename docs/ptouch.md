# Brother P-touch raster protocol

What Tavletta knows about printing on Brother P-touch tape printers (TZe
laminated tape, HSe heat-shrink tube). **Not yet tried on a printer**: no
P-touch was at hand. Everything below comes
from Brother's documents and from two open source projects, and is written
anew; no code of either project is included.

Sources:

- Brother's "Software Developer's Manual - Raster Command Reference" for the
  PT-E550W/P750W/P710BT (`cv_pte550wp750wp710bt_eng_raster_102.pdf`) and the
  PT-P900/P900W/P950NW/P910BT (`cv_ptp900_eng_raster_102.pdf`). Section
  numbers below refer to these.
- [ptouch-print](https://git.familie-radermacher.ch/linux/ptouch-print.git)
  by Dominic Radermacher (GPL-3.0-only): the older models, their USB
  product IDs, flags and quirks. Only these facts are used, no code.
- [ptouch](https://github.com/nbuchwitz/ptouch) by Nicolai Buchwitz
  (LGPL-2.1-or-later): the print position of heat-shrink tube, corrected
  there on printers. Only facts are used, no code.

Markers: **[spec]** in Brother's references, **[assumed]** likely but
untested, from one of the projects, or contradicting between the sources,
**[open]** unknown.

## Transport

- USB: vendor `04f9`, printer class, bulk in and out (Appendix A); the
  kernel's `usblp` driver offers it as `/dev/usb/lpN`, read and write.
  Tavletta uses that file (like for cab printers and the VC-500W), no
  libusb. The IEEE 1284 device ID names the model
  (`MFG:Brother;MDL:PT-P750W;...`); if the name is unknown, the USB product
  ID in sysfs (`idProduct`) does. [spec for the IDs, assumed for the
  device ID text]
- TCP port 9100 on the network models PT-E550W, PT-P750W, PT-P900W and
  PT-P950NW, the same byte stream. Brother's references describe the
  commands, not the network: whether the printer answers `ESC i S` and
  reports pages over TCP is [open]; Brother's network flow (5.6) only
  sends the data, the printer reads while it prints. Tavletta asks for the
  status:
  - none comes: it prints for the label's tape without waiting for the
    printer (the status row says that the printer does not answer). The
    model, and with it the head, comes from the last answer, the host name
    (`PT-P900W.local`) or the DNS-SD name of that address; only without
    any of these from the tape width (36 mm: PT-P900 series). [assumed]
  - it comes, but no page is reported: after the page's time the rest of
    the job is sent without waiting; the interface then says "Sent to the
    printer – it did not confirm printing", not "Printed" (also when no
    status came at all; `PrintResult.unconfirmed`).
    [assumed]
  - the printer closes the connection while Tavletta waits for a page: an
    error, with the number of labels reported printed so far; nothing more
    is sent.
  - the DNS-SD name is matched to the host in any case, with or without a
    final dot.
- Sending: the printer stops reading while it prints a page (5.6: "printer
  buffer is full, BUSY"), so a job is sent in pieces; it fails only when
  the printer takes no data for as long as a page may take (see Status).
  [spec]
- On USB the PT-P900 series prints while the data arrives ("concurrent
  printing", 5.1): it starts a page without waiting for the print command,
  and without compression a long page is 70 bytes per line, so the
  printer may wait for data in the middle of a page. [spec] Tavletta sends
  each page in one piece and reads the "printing completed" status before
  the next page, as in Brother's flow, so the buffers never back up.
- Search: USB through sysfs; on the network the DNS-SD service
  `_pdl-datastream._tcp` whose name contains one of these models
  ("Brother PT-P750W"). [assumed]
- Bluetooth (PT-P710BT, PT-D460BT, PT-P910BT, ...) is not supported:
  Tavletta has no Bluetooth connections. These models print over USB.
- Editor Lite: with the Editor Lite switch on, PT-P700, PT-P750W, PT-1230PC
  and PT-2430PC appear as a USB drive (product IDs 2064, 2065, 2030, 2031,
  from ptouch-print) and cannot print. Tavletta finds such a drive in
  `/sys/bus/usb/devices` (a Brother device without a printer interface),
  lists it in the search and explains how to switch Editor Lite off. The
  PT-P750W printer is 2062 (Appendix A); ptouch lists 2065 for it, which
  ptouch-print calls its Editor Lite drive: Tavletta follows Brother and
  ptouch-print. [spec for 2062]

## Models

Flags as ptouch-print documents them [assumed]: **PB** TIFF compression
(PackBits) needed, **P700** switch to raster with `ESC i a 01` and drop the
answer, **INFO** send the print information `ESC i z`, **CUT** auto cutter
(`ESC i M 40`), **D460** PT-D460BT family: fixed margin bytes. **Doc**: a
model of Brother's references, driven with the full command set (cut, half
cut, chain printing, margin, high resolution, heat-shrink tube).

| Model | USB ID | Head | Flags | Notes |
|---|---|---|---|---|
| PT-9200DX | 2001, 2002 | 384 pins, 360 dpi | PB, CUT | |
| PT-9700PC | 203c | 384, 360 dpi | PB, CUT, P700, INFO | |
| PT-2300 | 2004 | 112, 180 dpi | PB, CUT | |
| PT-2420PC | 2007 | 128, 180 dpi | PB | |
| PT-2450PC | 2011 | 128 | PB | |
| PT-1950 | 2019 | 112 | PB | unconfirmed in ptouch-print |
| PT-2700 | 201f | 128 | CUT | |
| PT-1230PC | 202c | 128 | | ptouch-print notes "more than 76 pixels, the first 32 blank"; unclear whether pins or columns, not applied [open] |
| PT-2430PC | 202d | 128 | CUT | |
| PT-2730 | 2041 | 128 | | 48 blank columns first [assumed] |
| PT-H500 | 205e | 128 | PB, CUT | |
| PT-E500 | 205f | 128 | PB | |
| PT-E550W | 2060 | 128 | Doc, PB, network | ptouch-print: does not work; ptouch: works with compression on |
| PT-P700 | 2061 | 128 | PB, P700, CUT | |
| PT-P750W | 2062 | 128 | Doc, PB, network | |
| PT-D450 | 2073 | 128 | INFO | |
| PT-D600 | 2074 | 128 | PB | |
| PT-P900 | 2083 | 560, 360 dpi | Doc | |
| PT-P900W | 2085 | 560, 360 dpi | Doc, network | |
| PT-P950NW | 2086 | 560, 360 dpi | Doc, network | |
| PT-P710BT | 20af | 128 | Doc, PB | no half cut, no `ESC i A`, no heat-shrink tube, `ESC i !` |
| PT-P910BT | 20c7 | 560, 360 dpi | Doc | no high resolution, no heat-shrink tube, `ESC i !` |
| PT-D410 | 20df | 128 | INFO, CUT, D460 | |
| PT-D460BT | 20e0 | 128 | P700, INFO, CUT, D460 | |
| PT-D610BT | 20e1 | 128 | P700, INFO, CUT, D460 | |
| PT-E310BT | 2201 | 128 | P700, INFO, D460 | |
| PT-E560BT | 2203 | 128 | P700, INFO, D460 | ptouch-print waits up to 10 s for answers |

- USB IDs of the documented models: Appendix A of both references [spec];
  the others from ptouch-print [assumed].
- Compression on the documented models: on for the PT-E550W, P750W and
  P710BT, off for the PT-P900 series, as ptouch sets it; Brother allows both
  (`M 00`, `M 02`). [spec for both; assumed for the choice]
- Model byte (status offset 4), used on TCP where no device ID names the
  model [spec]: PT-E550W 0x66 ("f"), PT-P750W 0x68 ("h"), PT-P900W 0x69
  ("o"), PT-P950NW 0x70 ("p"), PT-P900 0x71 ("q"), PT-P910BT 0x78 ("x").
  PT-P710BT 0x76 is not in the reference [assumed]. A network printer with
  another byte is treated as a documented model with a 128-pin head, or a
  560-pin head with 36 mm tape. [assumed]
- A USB P-touch whose model Tavletta does not know is refused ("does not
  know this printer model"); a model marked as unable to print raster
  graphics would be refused with its own message (none is at the moment).

## Print area

A raster line covers the whole head. Brother's tables (2.3.5 Raster line)
give, per tape, the pins for the left margin, the print area and the pins
for the right margin; the right margin is at the first byte of the line,
the left margin at the last [spec]. Tavletta renders the label at exactly
the printable height, so the design fills the printable part of the tape;
the outer edges of the tape stay blank.

| Tape | 128 pins, 180 dpi: left / print / right | 560 pins, 360 dpi: left / print / right |
|---|---|---|
| 3.5 mm (status: 4) | 52 / 24 / 52 | 248 / 48 / 264 |
| 6 mm | 48 / 32 / 48 | 240 / 64 / 256 |
| 9 mm | 39 / 50 / 39 | 219 / 106 / 235 |
| 12 mm | 29 / 70 / 29 | 197 / 150 / 213 |
| 18 mm | 8 / 112 / 8 | 155 / 234 / 171 |
| 24 mm | 0 / 128 / 0 | 112 / 320 / 128 |
| 36 mm | - | 45 / 454 / 61 |

- So on the 560-pin head the print area starts 264, 256, ..., 61 pins after
  the first bit of a line. ptouch-print uses the same position (centre
  at pin 272 counted from the last byte).
- ptouch-print uses wider areas for 9, 12 and 18 mm (52, 76, 120 pins);
  Tavletta uses Brother's numbers.
- Heads of 112 and 384 pins (older models): Brother's printable width at
  the head's resolution, centred, at most the head. [assumed]
- Heat-shrink tube (media type 0x11 for 2:1, 0x17 for 3:1): Brother's print
  area [spec]; the position from ptouch, 1 or 2 pins off Brother's
  numbers; for 560 pins ptouch took it from an analysis of Brother's own
  software, for 128 pins the source is not stated [assumed]:
  - 128 pins (Brother left / print / right, Tavletta's first pin): 2:1
    5.8 mm (status 6) 50/28/50, 52; 8.8 mm (9) 40/48/40, 42; 11.7 mm (12)
    31/66/31, 33; 17.7 mm (18) 11/106/11, 13; 23.6 mm (24) 0/128/0, 0; 3:1
    5.2 mm (5) 54/20/54, 56; 9.0 mm (9) 42/44/42, 44; 11.2 mm (11)
    39/50/39, 41; 21.0 mm (21) 4/120/4, 6.
  - 560 pins: 2:1 244/56/260, 261; 224/96/240, 241; 206/132/222, 223;
    166/212/182, 183; 144/256/160, 161; 3:1 252/40/268, 269; 228/88/244,
    245; 222/100/238, 239; 152/240/168, 169; 31 mm 92/360/108, 109.
  - Not on the PT-P710BT and PT-P910BT. Tube up to 500 mm long (2.3.4).
- In Tavletta a tube is designed on the nearest tape width (11.2 mm on
  12 mm); the job uses the tube's own print area.
- FLe labels (media 0x13, 21 x 45 mm die-cut, PT-P900/P900W/P950NW): the
  status says width 21 and length 45 (2Dh) [spec]. Brother gives no print
  area for them; Tavletta uses that of the 21 mm tube (left 152, print 240,
  right 168 pins) [assumed]. They show as 21 mm, so a label designed on
  another width is scaled to them after the usual warning.

## Raster lines

- One raster line per column of the label, the first column first. Image
  row y is pin `first + y`, counted from the first byte's highest bit
  ("MSB LSB, 1st B, 2nd B", 2.3.5). Black is 1. [spec]
- Bytes per line: head pins / 8 (16 for 128 pins, 70 for 560). [spec]
- `G n1 n2 data`: one line, n = length of data (16 bit, low byte first).
  Compressed (`M 02`): data is the PackBits-packed line, `Z` an empty line.
  [spec]
- Older PB models: each line is one PackBits literal piece
  (`G len+1 00 len-1 bytes`), as ptouch-print sends it. [assumed]
- Rendering: 1 bit per dot with a threshold at half grey, no dithering, so
  that codes and text stay sharp; photos print as black and white areas.
- High resolution (ESC i K bit 6): 180 x 360 dpi on the 128-pin models,
  360 x 720 dpi on the PT-P900, P900W and P950NW, there on laminated tape
  only and with `n2 = 09h` in ESC i z; the PT-P910BT has none [spec].
  Tavletta renders the label at twice the resolution and halves it across
  the tape, so the extra lines carry real detail; the line count and the
  margin are given in the finer dots. 2D codes are laid out for the
  halving (modules and their first row on even rows of the fine render,
  `render(across_step=2)`), so each module is a whole number of pins across
  and twice as many columns along: never averaged.

## Command sequence

Documented models [spec, sections 2.1 and 4]:

```
00 x 100 | 00 x 200            invalidate, once per job: 100 bytes on the
                               PT-E550W/P750W/P710BT, 200 on the PT-P900
                               series (2.1 of each reference)
1B 40                          ESC @ initialise, once per job
per page:
  1B 69 61 01                  ESC i a 1: raster mode
  [1B 69 21 00]                ESC i !: status notifications while printing;
                               PT-P910BT and PT-P710BT (not E550W/P750W)
  1B 69 7A n1 n2 w 00 L L L L p 00
                               ESC i z print information: n1 0x84 (recover,
                               width valid) on tape, 0x86 (also media) on
                               tube; n2 0x11 / 0x17 for tube, 0x09 for high
                               resolution on the PT-P900 series, else 0;
                               w tape width as in the status; L lines
                               (32 bit); p 0 first page, 1 others, on the
                               PT-P900 series 2 on the last (also a single) page
  1B 69 4D 40|00               ESC i M: auto cut on/off
  1B 69 41 01                  ESC i A: cut after every label (with auto
                               cut; not on the PT-P710BT)
  1B 69 4B k                   ESC i K: 0x04 half cut, 0x08 no chain
                               printing (feed and cut at the end), 0x40
                               high resolution
  1B 69 64 lo hi               ESC i d: margin (feed) in dots
  4D 02|00                     M: compression
  G ... / Z                    raster lines
  0C                           print, more pages follow
1A                             print the last page, feed and cut
```

- Cut: **Full cut** auto cut on, each label cut off. **Half cut** auto cut
  off with half cut on: the backing holds the labels together. **No cut**:
  one strip. Without chain printing the strip is fed and cut after the
  last label; chain printing leaves that out (it is cut when the next
  print starts). [spec] The cutter sits behind the print head: at least
  24.5 mm (PT-E550W/P750W/P710BT) or 27 mm (PT-P900 series) of tape come
  out per print (2.3.4). [spec]
- Margin (2.3.3), also in the print dialog per model: 14 to 900 dots at 180 dpi (2 to 127 mm) on the 128-pin
  models, 14 to 1800 dots at 360 dpi (1 to 127 mm) on the PT-P900 series,
  doubled at high resolution. Default 2 mm. [spec]
- Several copies: one job, a page per copy.

Older models (ptouch-print's subset) [assumed]:

```
00 x 100, 1B 40                invalidate, initialise (once per job)
[4D 02]                        PB: compression
1B 69 52 01 | 1B 69 61 01      ESC i R 1 (raster graphics), or ESC i a 1 (P700);
                               a P700 answer is read and dropped
per page:
  [1B 69 7A 00 00 w 00 L L L L n 00]
                               INFO: print information, no valid flags,
                               n = 2 on the D460 family, else 0
  [1B 69 64 01 00 4D 00]       D460: margin 1 dot, then M 00 (no compression);
                               ptouch-print reads 4D as a mandatory byte of
                               ESC i d, the bytes are the same
  [1B 69 4D 40]                CUT and full cut: auto cut
  [1B 69 4B 00]                D460 and chain printing
  G ...                        raster lines
  0C | 1A                      0C between copies, 1A after the last;
                               chain printing: 0C after the last too;
                               D460 family: always 1A
```

- ptouch-print prints one label per run, so several copies in one job are
  Tavletta's extension of its sequence, following Brother's page
  structure. [assumed]
- No margin setting, no half cut, no high resolution on these models.
- Models without a cutter (cutter lever) offer no cut setting.
- Quirks: the PT-2730's blank columns are applied; "needs white padding"
  (PT-E500, PT-H500) has no number in the sources and is left to the
  label's own spacing. [open]

## Status

`1B 69 53` (ESC i S) after invalidate (200 bytes, as the model is not
known yet) and initialise; the printer answers
32 bytes [spec, 4.2]:

| Offset | Content |
|---|---|
| 0-3 | `80 20 42 30`: print head mark, size, "B", series |
| 4 | model |
| 6 | battery level (PT-P900 series) |
| 7 | extended error (PT-P900 series) |
| 8 | error information 1 |
| 9 | error information 2 |
| 10 | tape width in mm (4 for 3.5 mm, 0 without tape) |
| 11 | media type: 0x01 laminated, 0x03 non-laminated, 0x04 fabric, 0x11 heat-shrink 2:1, 0x13 FLe, 0x14 flexible ID, 0x15 satin, 0x17 heat-shrink 3:1, 0xFF incompatible |
| 18 | status type: 0x00 answer, 0x01 printing completed, 0x02 error, 0x04 turned off, 0x05 notification, 0x06 phase change |
| 19 | phase: 0x00 waiting, 0x01 printing |
| 24, 25 | tape and text colour |

- Errors and how Tavletta explains them; the first set in this order wins
  [spec, except where noted]:
  - 2/0x10 cover open: "cover open"
  - 1/0x01 no media, 1/0x02 end of media (PT-P900 series): "no cassette"
  - 2/0x01 replace media, media type 0xFF: "cassette not recognised"
  - 1/0x04 cutter jam: "jammed"
  - 2/0x40 black marking not detected (PT-P900 series, FLe labels): "cannot
    find the next label"
  - 2/0x20 overheating: "print head too hot"
  - 1/0x08 weak batteries: "batteries low"
  - 1/0x10 printer in use (not used by these models [assumed]): "busy"
  - extended error 0x10 FLe tape end: "no cassette"; 0x21 incompatible
    media: "cassette not recognised"; 0x1D high-resolution/draft printing
    error, 0x1E adapter pull/insert error: "the printer reported a problem"
  - the others (high-voltage adapter, buffers, communication, system
    error), and an error status without bits: "the printer reported a
    problem"
  - no tape width without error bits: "no cassette"
- Answers are searched for `80 20 42`; bytes before it (an answer cut
  short, the 16-byte answers some models give) are skipped, more than
  4 kB without a status is an error. [assumed for the 16-byte answers,
  from ptouch-print]
- While printing, the documented models report each printed page (status
  type 0x01, sent before the page is really out, 5.1) or an error (0x02)
  [spec]; the PT-P910BT and PT-P710BT only after `ESC i !` [spec]. Brother
  says not to send `ESC i S` while printing. Tavletta reads the report
  after each page before it sends the next; a page may take 30 s plus its
  length at 10 mm/s (twice that at high resolution) [assumed]. An error
  then says how many labels came out. On USB no report is an error, on the
  network see Transport. The older models are not waited for.
- The extended error byte (7) is read only on the PT-P900 series; the
  other reference marks it reserved. [spec]

## In Tavletta

- Backend id `ptouch`, "Brother P-touch". Tape widths 3.5, 6, 9, 12, 18,
  24 and 36 mm, 12 mm for a new label; the label's height is the tape, its
  length is free (4.4 to 1000 mm, tube up to 500 mm, 2.3.4; shorter
  labels are extended). The inserted tape is
  taken over for a new label.
- The backend keeps no printer state: the print dialog hands the printer as
  last asked (`PrintOptions.info`) to `prepare()`, which renders for its
  head; the job itself uses the status read just before it.
- Printer details: model, tape width, tape type, printable width.
- Print dialog: copies, cut (as the model can), chain printing, high
  resolution (where the model has it), tape feed (documented models).
- Settings per printer in `settings.json` under `printers.ptouch`: `cut`,
  `chain`, `high_res`, `feed_mm`.

## To check on a printer

```sh
lsusb -d 04f9: -v | grep -E 'idProduct|bInterfaceClass'
cat /sys/class/usbmisc/lp*/device/ieee1284_id
```

- [open] Does the device ID name the model as `PT-P750W`?
- [open] Does a network printer answer `ESC i S` and report pages on TCP?
- [open] The PT-P710BT's model byte.
- [open] Is the heat-shrink position of ptouch or of Brother's tables right?
