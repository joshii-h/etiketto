# Brother P-touch raster protocol

What Etiketto knows about printing on Brother P-touch tape printers (TZe
laminated tape, HSe heat-shrink tube). Etiketto has not printed on a
P-touch yet: no printer of the family was at hand. Everything below comes
from documents and from two open source projects, and is written anew; no
code of either project is included.

Sources:

- Brother's "Software Developer's Manual - Raster Command Reference" for the
  PT-E550W/P750W/P710BT (`cv_pte550wp750wp710bt_eng_raster_102.pdf`) and the
  PT-P900 series (`cv_ptp900_eng_raster_102.pdf`), as referenced and
  tabulated by ptouch.
- [ptouch-print](https://git.familie-radermacher.ch/linux/ptouch-print.git)
  by Dominic Radermacher (GPL-3.0-only): the list of models, USB product
  IDs, model flags and quirks. Only these facts are used, no code.
- [ptouch](https://github.com/nbuchwitz/ptouch) by Nicolai Buchwitz
  (LGPL-2.1-or-later): the command sequence of the documented models, the
  print area tables for tape and heat-shrink tube, cut, chain printing and
  high resolution. Only facts are used, no code.

Markers: **[spec]** in Brother's references or in both projects alike,
**[assumed]** likely but untested or contradicting between the sources,
**[open]** unknown.

## Transport

- USB: vendor `04f9`, printer class; the kernel's `usblp` driver offers it
  as `/dev/usb/lpN`, read and write. Etiketto uses that file (like for cab
  printers and the VC-500W), no libusb. The IEEE 1284 device ID names the
  model (`MFG:Brother;MDL:PT-P750W;...`); if the name is unknown, the USB
  product ID in sysfs (`idProduct`) does. [spec for the IDs, assumed for
  the device ID text]
- ptouch-print writes at most 128 bytes per USB transfer; `usblp` splits
  writes itself, so Etiketto writes the job in one go. [assumed]
- TCP port 9100 on the network models PT-E550W, PT-P750W, PT-P900W and
  PT-P950NW, the same byte stream; status answers come on the same
  connection. [spec]
- Search: USB through sysfs; on the network the DNS-SD service
  `_pdl-datastream._tcp` whose name contains one of these models
  ("Brother PT-P750W"). [assumed]
- Bluetooth (PT-P710BT, PT-D460BT, PT-P910BT, ...) is not supported:
  Etiketto has no Bluetooth connections. These models print over USB.
- Editor Lite: with the Editor Lite switch on, PT-P700, PT-P750W, PT-1230PC
  and PT-2430PC appear as a USB drive (product IDs 2064, 2065, 2030, 2031)
  and cannot print. Etiketto finds such a drive in
  `/sys/bus/usb/devices` (a Brother device without a printer interface),
  lists it in the search and explains how to switch Editor Lite off.
  ptouch-print calls 2065 the Editor Lite mode of the PT-P750W, ptouch the
  printer itself: a 2065 with a printer interface is taken as the printer,
  one without as Editor Lite. [assumed]

## Models

Flags as ptouch-print documents them [spec]: **PB** TIFF compression
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
| PT-1230PC | 202c | 128 | | 32 blank columns first, at least 77 columns [assumed] |
| PT-2430PC | 202d | 128 | CUT | |
| PT-2730 | 2041 | 128 | | 48 blank columns first |
| PT-H500 | 205e | 128 | PB, CUT | |
| PT-E500 | 205f | 128 | PB | |
| PT-E550W | 2060 | 128 | Doc, PB, network | ptouch-print: does not work; ptouch: works with compression on |
| PT-P700 | 2061 | 128 | PB, P700, CUT | |
| PT-P750W | 2062 (2065) | 128 | Doc, PB, P700, network | |
| PT-D450 | 2073 | 128 | INFO | |
| PT-D600 | 2074 | 128 | PB | |
| PT-P900 | 2083 | 560, 360 dpi | Doc | |
| PT-P900W | 2085 | 560, 360 dpi | Doc, P700, network | |
| PT-P950NW | 2086 | 560, 360 dpi | Doc, network | |
| PT-P710BT | 20af | 128 | Doc, PB | no half cut, no heat-shrink tube |
| PT-P910BT | 20c7 | 560, 360 dpi | Doc | no heat-shrink tube |
| PT-D410 | 20df | 128 | INFO, CUT, D460 | |
| PT-D460BT | 20e0 | 128 | P700, INFO, CUT, D460 | |
| PT-D610BT | 20e1 | 128 | P700, INFO, CUT, D460 | |
| PT-E310BT | 2201 | 128 | P700, INFO, D460 | |
| PT-E560BT | 2203 | 128 | P700, INFO, D460 | ptouch-print waits up to 10 s for answers |

- Compression on the documented models as ptouch sets it: on for the
  PT-E550W, P750W and P710BT, off for the PT-P900 series (the PT-P900W
  would take it, ptouch-print sends it). [spec]
- High resolution on all documented models. [assumed]
- Model byte (offset 4) of the status, used on TCP where no device ID
  names the model: PT-E550W 0x65, PT-P750W 0x68, PT-P900W 0x69,
  PT-P950NW 0x70, PT-P900 0x71, PT-P710BT 0x76, PT-P910BT 0x78. [assumed]
  A network printer with another byte is treated as a documented model
  with a 128-pin head, or a 560-pin head with 36 mm tape. [assumed]
- A USB P-touch whose model Etiketto does not know is refused ("cannot
  print on this printer model"), and so would be a model marked as unable
  to print raster graphics (none is at the moment). [assumed]

## Print area

Each raster line covers the whole head; Brother's tables give the blank
pins before the print area and its size for each tape [spec]. Etiketto
renders the label at exactly the printable height, so the design fills the
printable part of the tape; the outer edges of the tape stay blank.

| Tape | 128 pins, 180 dpi: before / printable / after | 560 pins, 360 dpi |
|---|---|---|
| 3.5 mm (status: 4) | 52 / 24 / 52 | 248 / 48 / 264 |
| 6 mm | 48 / 32 / 48 | 240 / 64 / 256 |
| 9 mm | 39 / 50 / 39 | 219 / 106 / 235 |
| 12 mm | 29 / 70 / 29 | 197 / 150 / 213 |
| 18 mm | 8 / 112 / 8 | 155 / 234 / 171 |
| 24 mm | 0 / 128 / 0 | 112 / 320 / 128 |
| 36 mm | - | 45 / 454 / 61 |

- The 560-pin head centres the area at pin 272, not 280. [spec]
- ptouch-print uses wider areas for 9, 12 and 18 mm (52, 76, 120 pins);
  Etiketto uses Brother's numbers. [spec]
- Heads of 112 and 384 pins (older models): Brother's printable width at
  the head's resolution, centred, at most the head. [assumed]
- Heat-shrink tube (media type 0x11 for 2:1, 0x17 for 3:1), from ptouch,
  corrected there on hardware [spec]:
  - 128 pins: 2:1 5.8 mm (status 6) 52/28/48, 8.8 mm (9) 42/48/38,
    11.7 mm (12) 33/66/29, 17.7 mm (18) 13/106/9, 23.6 mm (24) 0/128/0;
    3:1 5.2 mm (5) 56/20/52, 9.0 mm (9) 44/44/40, 11.2 mm (11) 41/50/37,
    21.0 mm (21) 6/120/2.
  - 560 pins: 2:1 261/56/243, 241/96/223, 223/132/205, 183/212/165,
    161/256/143; 3:1 269/40/251, 245/88/227, 239/100/221, 169/240/151,
    31 mm 109/360/91.
  - Not on the PT-P710BT and PT-P910BT.
- In Etiketto a tube is designed on the nearest tape width (11 mm on
  12 mm); the job uses the tube's own print area. [assumed]

## Raster lines

- One raster line per column of the label, the first column first. Image
  row y is pin `before + y`; pins are counted from the first byte's highest
  bit. Black is 1. [spec]
- Bytes per line: head pins / 8 (16 for 128 pins, 70 for 560). [spec]
- `G n1 n2 data`: one line, n = length of data (16 bit, low byte first).
  Compressed (`M 02`): data is the PackBits-packed line, `Z` an empty line.
  [spec]
- Older PB models: each line is one PackBits literal piece
  (`G len+1 00 len-1 bytes`), as ptouch-print sends it. [spec]
- Rendering: 1 bit per dot with a threshold at half grey, no dithering, so
  that codes and text stay sharp; photos print as black and white areas.
- High resolution: twice the lines along the tape (180 x 360 dpi, PT-P900
  series 360 x 720). Etiketto renders the label at twice the resolution and
  halves it across the tape, so the extra lines carry real detail. ptouch
  sends each line twice instead. [assumed]

## Command sequence

Documented models (Brother's references, as ptouch sends them) [spec]:

```
00 x 200                       invalidate
1B 40                          ESC @ initialise
1B 69 61 01                    ESC i a 1: raster mode
per page:
  1B 69 7A 86 m w 00 L L L L p 00
                               ESC i z print information: 0x86 = recover,
                               width and media valid; m media type (0x11 or
                               0x17 for heat-shrink tube, else 0); w tape
                               width as in the status; L lines (32 bit);
                               p 0 on the first page, 1 on the others [assumed]
  1B 69 4D 40|00               ESC i M: auto cut on/off
  1B 69 41 01                  ESC i A: cut after every label (with auto cut)
  1B 69 4B k                   ESC i K: 0x04 half cut, 0x08 no chain
                               printing (feed and cut at the end), 0x40
                               high resolution
  1B 69 64 lo hi               ESC i d: margin (feed) in dots at 180 or 360 dpi,
                               doubled for high resolution
  4D 02|00                     M: compression
  G ... / Z                    raster lines
  0C                           print, more pages follow
1A                             print the last page, feed and cut
```

- Cut [spec, ptouch]: **Full cut** auto cut on, each label cut off.
  **Half cut** auto cut off with half cut on: the backing holds the labels
  together, the strip is cut after the last. **No cut**: one strip, cut
  after the last label. Chain printing leaves out the feed after the last
  label (it is cut when the next print starts), saving about 25 mm of tape.
- The cutter sits behind the print head: the first label after switching
  on starts with a blank piece of about 25 mm. [spec]
- Margin: 2 mm by default (Brother's 14 dots at 180 dpi), 2 to 127 mm.
  [spec]
- Several copies: one job, a page per copy.
- Models that answer `ESC i a` (P700 flag): the job is sent up to there,
  an answer of 32 bytes is read and dropped if one comes within 0.5 s, then
  the rest. [spec, assumed for the documented models with the flag]

Older models (ptouch-print's subset) [spec]:

```
00 x 100, 1B 40                invalidate, initialise
[4D 02]                        PB: compression
1B 69 52 01 | 1B 69 61 01      ESC i R 1 (raster graphics), or ESC i a 1 (P700)
[1B 69 7A 00 00 w 00 L L L L n 00]
                               INFO: print information, no valid flags,
                               n = 2 on the D460 family, else 0
[1B 69 64 01 00 4D 00]         D460: margin 1 dot, then M 00 (no compression);
                               ptouch-print reads 4D as a mandatory byte of
                               ESC i d, the bytes are the same
[1B 69 4D 40]                  CUT and full cut: auto cut
[1B 69 4B 00]                  D460 and chain printing
G ...                          raster lines
0C | 1A                        0C: chain printing (not on the D460 family)
```

- A job per copy, as ptouch-print prints one label per run. Between copies
  `1A` with full cut, else `0C`, so that the labels form one strip.
  [assumed]
- No margin setting, no half cut, no high resolution on these models.
- Models without a cutter (cutter lever) offer no cut setting.
- Quirks with known numbers are applied (PT-2730, PT-1230PC); "needs white
  padding" (PT-E500, PT-H500) has no number in the sources and is left to
  the label's own spacing. [open]

## Status

`1B 69 53` (ESC i S) after invalidate and initialise; the printer answers
32 bytes [spec]:

| Offset | Content |
|---|---|
| 0-3 | `80 20 42 30`: print head mark, size, "B", series |
| 4 | model |
| 8 | error information 1 |
| 9 | error information 2 |
| 10 | tape width in mm (4 for 3.5 mm, 0 without tape) |
| 11 | media type: 0x01 laminated, 0x03 non-laminated, 0x04 fabric, 0x11 heat-shrink 2:1, 0x13 flexible, 0x14 flexible ID, 0x15 satin, 0x17 heat-shrink 3:1, 0xFF incompatible |
| 18 | status type: 0x00 answer, 0x01 printing completed, 0x02 error, 0x05 notification, 0x06 phase change |
| 19 | phase: 0x00 waiting, 0x01 printing |
| 24, 25 | tape and text colour |

- Error bits and how Etiketto explains them; the first set in this order
  wins [spec for no media, cutter jam, weak batteries, replace media, cover
  open, overheating, system error; assumed for the rest, as on Brother QL
  printers]:
  - 2/0x10 cover open: "cover open"
  - 1/0x01 no media, 1/0x02 end of media: "no cassette"
  - 2/0x01 replace media, media type 0xFF: "cassette not recognised"
  - 1/0x04 cutter jam, 2/0x40 feeding error: "jammed"
  - 2/0x20 overheating: "print head too hot"
  - 1/0x08 weak batteries: "batteries low"
  - 1/0x10 printer in use: "busy"
  - the others (high-voltage adapter, fan, buffers, communication, system
    error), and an error status without bits: "the printer reported a
    problem"
  - no tape width without error bits: "no cassette"
- Answers are searched for `80 20 42`; bytes before it (an answer cut
  short, the 16-byte answers some models give) are skipped, more than
  4 kB without a status is an error. [spec for the 16-byte answers]
- After a job, the documented models report each printed page (status type
  0x01) or an error (0x02). Etiketto waits for them, at most 60 s without
  any status; an error then says how many labels came out. The older models
  are not waited for. [assumed]

## In Etiketto

- Backend id `ptouch`, "Brother P-touch". Tape widths 3.5, 6, 9, 12, 18,
  24 and 36 mm; the label's height is the tape, its length is free (up to
  1 m [assumed]). The inserted tape is taken over for a new label.
- Printer details: model, tape width, tape type, printable width.
- Print dialog: copies, cut (as the model can), chain printing, high
  resolution (documented models), tape feed (documented models).
- Settings per printer in `settings.json` under `printers.ptouch`: `cut`,
  `chain`, `high_res`, `feed_mm`.

## To check on a printer

```sh
lsusb -d 04f9: -v | grep -E 'idProduct|bInterfaceClass'
cat /sys/class/usbmisc/lp*/device/ieee1284_id
```

- [open] Does the device ID name the model as `PT-P750W`?
- [open] Do the model bytes on TCP match the table above?
- [open] Half cut and "No cut": is the strip cut after the last label?
- [open] Do the documented models report every page on USB and TCP?
