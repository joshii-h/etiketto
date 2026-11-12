# EPL2: older Zebra label printers

How Etiketto talks to printers that speak EPL2 (Eltron Programming
Language): Zebra LP/TLP 2824 and 2844, GK420 and GX420 set to EPL, and
compatibles. **Untested on hardware**: no EPL2 printer was available while
this was written.

The byte sequences follow [LPrint](https://github.com/michaelrsweet/lprint)
by Michael R Sweet (Apache-2.0, `lprint-epl2.c`); Etiketto reimplements them
in Python, no LPrint code is included. Zebra's "EPL2 Programming Guide"
describes the commands.

Markers: **[LPrint]** as LPrint sends it, **[spec]** from Zebra's manual,
**[assumed]** likely but untested, **[open]** unknown.

## Transport

- TCP port 9100, or USB (`/dev/usb/lpN`). [spec]
- Found on USB when the IEEE 1284 command set names `EPL` (LPrint matches
  `COMMAND SET:EPL;`), on the network when `usb_CMD` in the mDNS TXT record
  does. [LPrint] [assumed]
- No status: LPrint asks nothing, and so does Etiketto. A printer that
  accepts the connection is "reachable"; its problems show on the printer
  only. `UQ` (configuration) and `^ee` (error status) exist. [LPrint] [open]

## A print job

```
(empty line)
N                         (clear the image buffer; "\nN\n" marks the job as EPL2)
D7                        (density 0 to 15, only if darkness is set)
q16                       (label width in dots, whole bytes)
Q3,24                     (label length and gap in dots)
GW0,0,2,1                 (one row: x, y, bytes per row, height 1)
<2 raw bytes>
GW0,1,2,1
<2 raw bytes>
P1                        (number of labels)
```

- Raster polarity: 0 = black, 1 = white (LPrint renders EPL2 with white as
  1). [LPrint] [assumed: confirmed by the manual as far as remembered]
- One `GW` per row with ink; rows without ink are not sent. LPrint's test
  skips rows of `0x00`, which in this polarity are black rows, and sends the
  white ones: a bug. Etiketto skips white rows (`0xFF`). [LPrint]
- `Q`: LPrint sends none and trusts the printer's setup. Etiketto sends the
  label length and the gap from the label (`Q3,24`), `B` before the number
  for black marks (`Q3,B24`) and `0` for continuous media (`Q3,0`). [spec]
  [assumed]
- `P` with the number of copies; LPrint sends `P1`. [spec]
- Darkness: `D` is 15 × per cent / 100. [LPrint]
- Not sent: speed (`S` takes a model-specific code, not inches per second;
  LPrint sends inches per second there), the cutter (LPrint sends `C`, which
  in EPL2 is a counter, not a cut). [spec] [assumed]
- Example, 16 dots, two rows of 12 black dots and a white row:
  `\nN\nq16\nQ3,24\nGW0,0,2,1\n\x00\x0f\nGW0,1,2,1\n\x00\x0f\nP1\n`.

## Settings in Etiketto

| Setting | Values | Command |
|---|---|---|
| Resolution | 203, 300 dpi | renders at 8 or 12 dots per mm |
| Label sensor | gap, black mark, continuous | `Q…,gap`, `Q…,Bmark`, `Q…,0` |
| Darkness | as set on the printer, 0 to 100 % | `D0` to `D15` |

Widest label: 104 mm (4 inch head at 203 dpi). [spec]
