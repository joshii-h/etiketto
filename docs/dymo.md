# DYMO LabelWriter

How Etiketto talks to DYMO LabelWriter printers: 300, 310, 315, 320, 330,
400, 450 (Turbo, Twin Turbo, DUO label side) and 4XL. **Untested on
hardware**: no LabelWriter was available while this was written.

The byte sequences follow [LPrint](https://github.com/michaelrsweet/lprint)
by Michael R Sweet (Apache-2.0, `lprint-dymo.c`, which goes back to the
CUPS DYMO driver); Etiketto reimplements them in Python, no LPrint code is
included. Roll selection, label length and status follow DYMO's
"LabelWriter 450 Series Technical Reference Manual".

Markers: **[LPrint]** as LPrint sends it, **[spec]** from DYMO's technical
reference, **[assumed]** likely but untested, **[open]** unknown.

## Transport

- USB (`/dev/usb/lpN`); TCP port 9100 for networked models such as the
  LabelWriter 450 with a print server. [assumed]
- Found on USB by the IEEE 1284 device ID: maker DYMO and a model with
  "LabelWriter" (`MFG:DYMO;MDL:LabelWriter 450;`). [LPrint]
- The LabelWriter 550 and 5XL only print on DYMO's labels with a chip and
  are left out, as in LPrint. The LabelManager tape printers use another
  dialect and are not supported. [LPrint]
- On the network the model comes from the mDNS announcement (the service
  name or `usb_MDL`), looked up once per address when the printer was not
  found by a search (also when nothing answers: not again on each
  refresh): a 4XL has the wider head. [assumed]

## Status

| Request | Answer | Meaning |
|---|---|---|
| `ESC A` | 1 byte | bit 0 ready, bit 1 top of form, bit 5 no paper, bit 6 paper jam, bit 7 error; ready is `03` |

- Bits 2 to 4 are unused; an answer with one of them set, or more than one
  byte, is not a status. No answer within 1 s: "no status", the job is
  sent anyway. LPrint asks nothing. [spec] [assumed: not tried on a
  printer]
- No paper comes with the error bit; Etiketto names "no paper" first.

## A print job

```
ESC × 200, ESC @         1B … 1B 40      end any half command, reset
per label:
  ESC Q 0 0              1B 51 00 00     (as LPrint sends it)
  ESC B 0                1B 42 00        no dot tab: the label starts at the first dot
  ESC L hi lo            1B 4C 01 2C     longest feed to the next label in dots (big endian)
  ESC D n                1B 44 02        bytes per row
  ESC q "1"              1B 71 31        roll: ASCII "0" automatic, "1" left, "2" right
  ESC c|d|e|g            1B 64           density, only if darkness is set
  ESC f 1 n              1B 66 01 n      skip n blank rows before a row with ink
  SYN data               16 …            one row with ink
  ESC E                  1B 45           form feed to the next label
```

- 300 dpi across the head and along the feed; raster polarity 1 = black.
  [LPrint]
- `ESC L` is "the maximum distance the printer should travel while
  searching for the top-of-form hole", "usually set to a value slightly
  longer than the true label length" (DYMO). LPrint sends the label's own
  length; Etiketto sends label, gap and 3 mm. [spec]
- `ESC q`: the roll is an ASCII digit by DYMO's manual; LPrint sends a
  binary 1. Etiketto sends `"1"`, which single-roll printers ignore. [spec]
- Blank rows before a row with ink are skipped with `ESC f`; blank rows at
  the end are not sent, the form feed passes them. [LPrint]
- Darkness in per cent: 3 × per cent / 100 selects `c` (lightest), `d`, `e`
  (the printer's normal) or `g` (darkest). [LPrint]
- Copies are pages: the reset once, then one label after the other.
- "Print upside down" turns the image by 180°. [open]
- Example from LPrint (rows `FF 00`, blank, `00 FF`, darkness 50 %, length
  as LPrint sends it):
  `1B×200 1B 40 1B 51 00 00 1B 42 00 1B 4C 00 03 1B 44 02 1B 71 01 1B 64 16 FF 00 1B 66 01 01 16 00 FF 1B 45`;
  Etiketto sends `1B 71 31` for the roll. Its tests check the rest byte
  for byte.

## Print head

- LabelWriter 4xx: 672 dots (56.9 mm); 4XL: 1248 dots (105.7 mm). Etiketto
  takes the 4XL's width when the USB model name contains "4XL". [spec]
  [assumed]
- A label sold as 57 mm wide is one dot wider than the 4xx head: up to 1 mm
  more than the head is cut off at the edge, wider labels are refused.

## Label sizes offered

| DYMO | Width × length |
|---|---|
| 99010 | 28 × 89 mm |
| 99012 | 36 × 89 mm |
| 11352 | 25 × 54 mm |
| 11355 | 19 × 51 mm |
| 11354 | 57 × 32 mm |
| 99014 | 54 × 101 mm |
| 1744907 (4XL) | 104 × 159 mm |
