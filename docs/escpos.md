# ESC/POS: receipt printers on 58 and 80 mm rolls

How Etiketto prints labels on receipt printers that speak ESC/POS: Epson's
TM series and the many compatible receipt printers. The label is printed on
endless paper, as long as its content. **Untested on hardware**: no ESC/POS
printer was available while this was written.

The byte sequences follow [LPrint](https://github.com/michaelrsweet/lprint)
by Michael R Sweet (Apache-2.0, `lprint-escpos.c`); Etiketto reimplements
them in Python, no LPrint code is included. Status and cut follow Epson's
"ESC/POS Command Reference".

Markers: **[LPrint]** as LPrint sends it, **[spec]** from Epson's reference,
**[assumed]** likely but untested, **[open]** unknown.

## Transport

- TCP port 9100, or USB (`/dev/usb/lpN`). [spec]
- Found on USB when the IEEE 1284 command set names `ESC/POS` (Epson:
  `MFG:EPSON;MDL:TM-T88V;CMD:ESC/POS;`); only by the command set, as Epson's
  inkjet printers have the same maker. On the network: `usb_CMD` in the
  mDNS TXT record of `_pdl-datastream._tcp`. [LPrint] [assumed]

## Paper and resolution

| Paper | Printable | Dots at 203 dpi | Dots at 180 dpi |
|---|---|---|---|
| 58 mm | 48 mm | 384 | 344 |
| 80 mm | 72 mm | 576 | 512 |

- The print head is narrower than the paper; Etiketto renders the label at
  the printable width. The numbers are the common ones; some models differ.
  [assumed]
- LPrint's default is 180 dpi, many compatible printers have 203 dpi;
  Etiketto's default is 203. [LPrint] [assumed]

## Status

| Request | Answer | Used for |
|---|---|---|
| `DLE EOT 2` (`10 04 02`) | 1 byte | `0x04` cover open, `0x20` stopped at the paper end, `0x40` error |
| `DLE EOT 4` (`10 04 04`) | 1 byte | `0x0C` paper near the end, `0x60` paper end |

- Every answer byte has bit 1 and 4 set and bit 0 and 7 clear; anything else
  is not an answer. [spec]
- LPrint asks `ESC v` instead (paper sensor). Etiketto uses the real-time
  `DLE EOT` questions, which many compatible printers answer; without an
  answer the state is "no status" and the job is sent anyway. [spec]
  [assumed]

## A print job

```
ESC @                        1B 40               reset
GS P x y                     1D 50 CB CB         motion units: one dot (here 203 dpi)
GS L 0 0                     1D 4C 00 00         no left margin
per label:
  ESC J n                    1B 4A n             feed blank rows (n up to 255 dots)
  GS v 0 0 xL xH yL yH data  1D 76 30 00 …       raster image: width in bytes, height in dots
  ESC J dpi                  1B 4A CB            feed an inch (past the cutter)
  GS V 0 | GS V 1            1D 56 00 | 1D 56 01 full or partial cut (if chosen)
ESC @                        1B 40
```

- `GS P`: Epson's printers count `ESC J` in 1/180 in, many compatible ones
  in 1/203 in; setting the motion units to the resolution makes the feeds
  dots on both. LPrint does not send it. [spec] [assumed]
- Raster polarity 1 = black, rows MSB first. A raster image carries at most
  32768 bytes; longer labels are sent in several. [LPrint]
- The label's start leaves the printer first: the label (x along the
  paper) is turned so that its top edge is at the right end of the head.
  "Print upside down" turns the label by 180°. [assumed]
- Sending and closing on TCP as described in [zpl.md](zpl.md#transport);
  on a receipt printer a long job takes as long as printing it.
- LPrint drops the blank rows at the start; Etiketto feeds them, as they are
  the label's own spacing. [LPrint]
- Cut: LPrint sends `ESC i`; Etiketto sends `GS V 0` (full) or `GS V 1`
  (partial), the cut of current printers, or nothing. A printer without a
  cutter ignores it. [spec] [assumed]
- Example from LPrint (16 dots, rows `AA AA` and `55 55`):
  `1B 40 1D 4C 00 00 1D 76 30 00 02 00 02 00 AA AA 55 55 1B 4A CB 1B 40`
  at 203 dpi without a cut; Etiketto adds `1D 50 CB CB` after the first
  `1B 40`. At 180 dpi the feed and unit bytes are `B4`.
- No darkness and no speed: ESC/POS has no common commands for them.
  [LPrint] [spec]

## Settings in Etiketto

| Setting | Values |
|---|---|
| Resolution | 203, 180 dpi |
| Paper width (tape width) | 58, 80 mm |
| Cut (print dialog) | full, partial, none |
| Print upside down | off, on |
