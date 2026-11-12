# ZPL: Zebra label printers and compatibles

How Etiketto talks to printers that speak ZPL (Zebra Programming Language):
Zebra's desktop and industrial printers (GK, GX, ZD, ZT series and others)
and the many printers that emulate ZPL. **Untested on hardware**: no ZPL
printer was available while this was written.

The byte sequences follow [LPrint](https://github.com/michaelrsweet/lprint)
by Michael R Sweet (Apache-2.0, `lprint-zpl.c`), which prints on these
printers; Etiketto reimplements them in Python, no LPrint code is included.
Zebra's "ZPL II Programming Guide" describes the commands.

Markers: **[LPrint]** as LPrint sends it, **[spec]** from Zebra's manual,
**[assumed]** likely but untested, **[open]** unknown.

## Transport

- TCP port 9100, or USB through the kernel's printer driver
  (`/dev/usb/lpN`); the same commands on both. [spec]
- Found on USB by the IEEE 1284 device ID: a command set (`CMD:`) with
  `ZPL`, or a maker starting with "Zebra" when the ID names no command set.
  Examples from LPrint: `MANUFACTURER:Zebra Technologies ;COMMAND SET:ZPL;MODEL:ZTC ZD621-203dpi ZPL;`.
  [LPrint]
- Found on the network by mDNS `_pdl-datastream._tcp`: `usb_CMD` in the TXT
  record names ZPL, or the service or `usb_MFG` starts with "Zebra".
  [assumed]
- Questions are answered on the same connection; Etiketto waits 1 s for
  each and treats silence as "no status" (state UNKNOWN), not as an error.
  [assumed]
- An answer that comes after its 1 s ends up in front of the next answer:
  the frames are told apart by their number of fields (`~HI` 5, `~HS` 12,
  11 and 2), not by their place.
- Sending: the timeout of 10 s counts from the last bytes the printer took,
  so a large job on a slow printer goes through; a job may take 10 s plus
  its size at 8 kB/s at most. A connection reset while sending is a
  printer that stopped taking data, not a missing printer.
- After the job Etiketto closes its sending side and reads and drops what
  the printer still says (closing with an unread answer would reset the
  connection and cut the job off). It is done once the printer has
  acknowledged every byte and then closed its side, stayed quiet for 1 s,
  or kept talking for 4 s more. A printer that takes nothing for 10 s, or
  resets or closes the connection before it has everything, is an error.

## Status and identification

| Request | Answer | Used for |
|---|---|---|
| `~HI` | `<STX>ZD621-203dpi,V93.21.18Z,8,8192KB<ETX><CR><LF>` | model, firmware, dots per mm (8, 12, 24 = 203, 300, 600 dpi) |
| `~HS` | three frames `<STX>…<ETX><CR><LF>` | paper out, paused, labels waiting (frame 1); head up, ribbon out, labels remaining (frame 2) |
| `~HQES` | text with `ERRORS: 1 00000000 00000005` | the second hex word is a bit field of errors |

- `~HI`: LPrint reads the model and dots per mm the same way. [LPrint]
- `~HS` frame 1 is `aaa,b,c,dddd,eee,…`: b paper out, c paused, dddd label
  length in dots, eee formats in the buffer. Frame 2 is
  `mmm,n,o,p,q,r,s,t,uuuuuuuu,…`: o head up, p ribbon out, uuuuuuuu labels
  remaining. LPrint reads only the label length. [spec] [assumed: field
  positions from the manual, not seen on a printer]
- `~HQES` error bits (from LPrint's header): `0x1` media out, `0x2`
  ribbon out, `0x4` head open, `0x8` cutter fault, `0x1000` paper jam,
  `0x10000` paused, `0x40000`/`0x80000` black mark not found. Etiketto
  asks it only when `~HS` was answered and showed no problem. [LPrint]
- Before a job Etiketto asks `~HS` (and `~HQES`) and stops if the printer
  reports a problem; a printer that does not answer gets the job anyway.
- Answers that do not have this shape (garbage, half an answer) count as no
  answer. The resolution "As reported by the printer" uses `~HI`; without an
  answer: with "As reported by the printer" each print asks `~HI` again;
  resolutions are kept per printer address, and the label is rendered for
  the one of the printer it is printed on. If the printer now reports
  another resolution, a whole ratio upwards (300 to 600 dpi) is scaled dot
  by dot, any other (600 to 300 or 203 to 300 dpi) renders the label again. A printer not heard
  of before gets 3 s for `~HI` instead of 1 s; one that never answered it
  gets no job, and Etiketto asks to choose the resolution instead of
  guessing.

## A print job

```
~SD15                                (darkness, only if set: 0 to 30)
~DGR:ETIKETTO.GRF,6,2,IF0:,          (the graphic: total bytes, bytes per row, data)
^XA
^MMT,Y                               (after printing, only if set: T tear, P peel, C cut)
^PR4,4,4                             (speed in inches per second, only if set)
^PON                                 (not turned: the first row leaves first)
^PW16                                (print width in dots)
^LH0,0
^LL3                                 (label length in dots)
^MNY                                 (sensor, only if set: Y gap, M black mark, N continuous)
^PQ1,0,1,N                           (number of labels)
^FO0,0^XGR:ETIKETTO.GRF,1,1^FS       (place the graphic)
^XZ
^XA^IDR:ETIKETTO.GRF^FS^XZ           (delete it again)
```

- The label is downloaded to the RAM drive (`R:`) with `~DG` and printed with
  `^XG`, not with `^GFA` or `:Z64:`. Rows are hex, 1 = black, the leftmost
  dot in the highest bit, padded with white to whole bytes. [LPrint]
- Compression: a run of a hex character is written as `z` per 400, one of
  `g`…`y` for 20 to 380, one of `G`…`Y` for 1 to 19, then the character;
  a row equal to the previous one is `:`; trailing zeros are `,` (an odd
  number of zeros leaves one `0` first). Example, 16 dots, rows `FF F0`,
  `FF F0`, `00 00`: `~DGR:LPRINT.GRF,6,2,IF0:,`. Etiketto's tests check
  this example byte for byte and decode random rows again. [LPrint] [spec]
- `^PON`: the first row of the image leaves the printer first, as in the
  other raw languages. LPrint sends `^POI` (turned by 180°). Which way
  round the label comes out has not been seen on a printer; "Print upside
  down" turns the image by 180° for a printer where it comes out the wrong
  way. [spec] [open]
- `^MN`: LPrint sends `^MNM` (black mark) for gap labels too; by the manual
  gap labels are `^MNY` ("web"), and Etiketto sends that. By default
  ("As set on the printer") it sends no `^MN` at all. [spec]
- `^LL`: LPrint sends the length only for continuous media; Etiketto sends
  it always, so the first label is right before the printer has calibrated.
  [assumed]
- `^PQ` with the number of copies in one job; LPrint prints one label per
  job. [spec]
- Darkness: the setting in per cent, `~SD` is 30 × per cent / 100. `~SD`,
  `^PR` and `^MM` are sent only when set in Etiketto; otherwise the
  printer's setup applies. [LPrint] [spec]
- Etiketto does not track the job: it is done once it is sent. [assumed]

## Settings in Etiketto

| Setting | Values | Command |
|---|---|---|
| Resolution | As reported by the printer, 203, 300, 600 dpi | renders at 8, 12 or 24 dots per mm |
| Label sensor | as set on the printer, gap, black mark, continuous | none, `^MNY`, `^MNM`, `^MNN` |
| After printing | as set on the printer, tear off, peel off, cut | none, `^MMT,Y`, `^MMP,N`, `^MMC,Y` |
| Darkness | as set on the printer, 0 to 100 % | `~SD00` to `~SD30` |
| Speed | as set on the printer, 1 to 12 in/s | `^PR` |
| Print upside down | off, on | the image turned by 180° |

Widest label: 104 mm, the 832 dots of a 4 inch head at 203 dpi. [spec]

## Not used

- `:Z64:` (zlib and base 64), shorter than hex for large labels.
- `^MT` (thermal transfer or direct), `~TA` (tear-off offset), `^LT`
  (top offset): the printer's setup stays in charge.
- Printer fonts and barcodes: Etiketto sends the rendered label as one
  graphic, so the preview stays exact.
