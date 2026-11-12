# TSPL: TSC and 4 × 6 in shipping label printers

How Etiketto talks to printers that speak TSPL or TSPL2: TSC's printers and
the many low-cost shipping label printers built on the same language
(Munbyn, iDPRT, Xprinter, Rollo, Phomemo, Marklife, …). **Untested on
hardware**: no TSPL printer was available while this was written.

The byte sequences follow [LPrint](https://github.com/michaelrsweet/lprint)
by Michael R Sweet (Apache-2.0, `lprint-tspl.c`), which prints on several of
these printers; Etiketto reimplements them in Python, no LPrint code is
included. The status and model questions come from TSC's "TSPL/TSPL2
Programming Manual".

Markers: **[LPrint]** as LPrint sends it, **[spec]** from TSC's manual,
**[assumed]** likely but untested, **[open]** unknown.

## Transport

- TCP port 9100 (TSC's network models), or USB (`/dev/usb/lpN`). Many of
  the low-cost printers are USB only; some show up as a USB serial or vendor
  device instead of a printer, and Etiketto does not find those. [spec]
  [assumed]
- Found on USB when the IEEE 1284 command set names `TSPL`, `TSPL2` or
  `XPP`/`XL`, the tag several compatible printers use (LPrint's examples:
  Rollo `MFG:Printer;MDL:Thermal Printer;CMD:XPP,XL;`, Marklife X4,
  Phomemo PM-241-BT), or the maker is TSC. On the network: the same in the
  mDNS TXT record of `_pdl-datastream._tcp`. [LPrint] [assumed]

## Status

| Request | Answer | Meaning |
|---|---|---|
| `ESC !?` | 1 byte | `0x00` ready; bits: `0x01` head open, `0x02` paper jam, `0x04` out of paper, `0x08` out of ribbon, `0x10` paused, `0x20` printing, `0x80` other error |
| `~!T` CR LF | model name, e.g. `TTP-244 Plus` | the name in the status row |

- LPrint asks nothing (its status function is empty). The two questions
  come from TSC's manual; many compatible printers answer neither.
  Etiketto waits 1 s for each: no answer means "no status", not an error,
  and the job is sent anyway. It asks `~!T` only from a printer that
  answered `ESC !?`. [spec] [assumed]
- Before a job, a printer that reports a problem gets no job.
- There is no resolution question: choose 203 or 300 dpi. [spec]

## A print job

```
SIZE 101.6 mm,152.4 mm          (label size in mm, from the image in dots)
GAP 3 mm,0 mm                   (only if set: gap labels; BLINE for black marks, GAP 0 mm for continuous)
DIRECTION 0,0                   (DIRECTION 1,0 with "Print upside down")
DENSITY 8                       (only if darkness is set: 0 to 15)
SPEED 4                         (only if set, inches per second)
SET CUTTER OFF                  (only if set, see below)
SET PEEL OFF
SET TEAR ON
CLS
BITMAP 0,0,2,8,1,<16 raw bytes>PRINT 1,1
```

- Commands end with LF, as LPrint sends them; the bitmap's bytes follow the
  last comma of `BITMAP x,y,bytes per row,height,mode` and `PRINT` follows
  the data without a separator (its length is known). [LPrint]
- Raster polarity: 0 = black, 1 = white. [LPrint]
- `GAP`: LPrint always sends 3 mm; Etiketto sends the label's gap. `BLINE`
  for black marks and `GAP 0 mm,0 mm` for continuous media; nothing with
  "As set on the printer", the default. [LPrint] [spec]
- `DIRECTION 0,0` as LPrint sends it for labels upright, `DIRECTION 1,0`
  ("Print upside down") turns the label by 180°. The printer keeps the
  direction in its memory, so Etiketto always sends one. Which way round
  the label comes out has not been seen on a printer. [LPrint] [spec]
  [open]
- Density is (per cent × 15 + 50) / 100. [LPrint]
- `PRINT copies,1`: all copies in one job. [LPrint]
- `SET TEAR`, `SET PEEL`, `SET CUTTER`: from the manual; LPrint sends none
  of them. The printer keeps them in its memory, so each mode switches the
  other two off: tear off is `SET CUTTER OFF`, `SET PEEL OFF`,
  `SET TEAR ON`; peel off `SET CUTTER OFF`, `SET TEAR OFF`, `SET PEEL ON`;
  cut `SET PEEL OFF`, `SET TEAR OFF`, `SET CUTTER 1`. [spec]
- Sending and closing on TCP as described in [zpl.md](zpl.md#transport).
- Example, 16 dots wide and 8 rows, each row 8 black and 8 white dots, at 8
  dots per mm, gap sensor chosen: `SIZE 2 mm,1 mm\nGAP 3 mm,0 mm\nDIRECTION 0,0\nCLS\nBITMAP 0,0,2,8,1,` then
  `00 FF` eight times, then `PRINT 1,1\n`.

## Settings in Etiketto

| Setting | Values | Command |
|---|---|---|
| Resolution | 203, 300 dpi | renders at 8 or 12 dots per mm |
| Label sensor | as set on the printer, gap, black mark, continuous | none, `GAP`, `BLINE`, `GAP 0 mm` |
| After printing | as set on the printer, tear off, peel off, cut | none, or the mode on and the other two off |
| Darkness | as set on the printer, 0 to 100 % | `DENSITY 0` to `15` |
| Speed | as set on the printer, 1 to 12 in/s | `SPEED` |
| Print upside down | off, on | `DIRECTION 0,0`, `DIRECTION 1,0` |

Widest label: 108 mm, TSC's print width for 4 inch heads. [spec]
