# cab printers: JScript over the network

How Etiketto talks to cab label printers with a 4 inch print head (EOS2,
EOS5, SQUIX 4, MACH 4S). JScript is cab's own printer language and is
publicly documented by cab: "JScript Programming Manual" for SQUIX, MACH 4S,
EOS2, EOS5, XC Q, XD Q, HERMES Q, PX Q, AXON 1/2, linked from
<https://www.cab.de/en/programming>. Nothing here is reverse engineered;
the device answers below were seen on an EOS2/300 with firmware
5.47.7 (Jan 22, 2026).

Markers: **[manual]** from cab's manual, **[confirmed]** seen on the device,
**[assumed]** likely but untested, **[open]** unknown.

## Transport

- TCP port 9100 ("RawIP"), with the interpreter for RawIP set to JScript
  (the default; the printer can also run ZPL there). [confirmed]
- Commands end with CR; Etiketto sends CR LF. [confirmed]
- ESC commands (`0x1B` + letter) are executed at once, also while a job is
  running, and answer on the same connection without a terminator unless
  stated. [manual] [confirmed]
- Status requests not more often than every 0.5 s. [manual]
- The printer announces itself by mDNS as `cab <model> (<MAC suffix>)`, e.g.
  `cab EOS2/300 (aa:bb:cc)`, with `_pdl-datastream._tcp` on port 9100,
  `_printer._tcp`, `_http._tcp` and `_ftp._tcp`; the TXT record has
  `usb_MFG=cab`, `usb_MDL=EOS2/300` and `pld=application/vnd.cab-jscript`.
  [confirmed]

## Status

| Request | Answer | Meaning |
|---|---|---|
| `ESC s` | `Y-000000N` (9 characters) | online (`Y`/`N`), error (`-` = none), labels still to print (6 digits), interpreter active (`Y`/`N`) |
| `ESC z` | `NNNNNNNNNNNNN` CR | paused, has a job, not ready for data, paper moving, ribbon warning, paper end warning, … (13 × `Y`/`N`) |
| `ESC l` | `Y0020` | labels synchronised (`Y`/`N`), measured distance from label to label in mm (`0000` = unknown) |
| `qv` CR | `5.47.7 Jan 22, 2026 (EOS2/300)` CR | firmware and model; the number after `/` is the resolution in dpi |
| `qp` CR | `NONE` CR | peripherals: `CUTTER`, `REWINDER`, … |

[manual] [confirmed]

- Error characters of `ESC s` that Etiketto names: `P` out of paper,
  `D` print head or pinch roller open, `E` synchronisation error (no label
  found), `F` out of ribbon, `R` ribbon in thermal direct mode. Others (`B`
  protocol error, `H` heating voltage, `O` out of memory, `W` head
  overheated, `Y`/`Z` print head error or damaged, …) are reported with the
  raw answer. [manual]
- After a text command such as `qv` the printer reports "interpreter
  active" (`Y-000000Y`) with no labels pending for about one second.
  ESC commands alone do not. Etiketto therefore waits for `N` before it
  sends a job. [confirmed]
- `ESC l` reports the distance of the loaded labels (label height + gap)
  once the printer has synchronised, e.g. after Feed. [manual] [confirmed]
- Widest label: 105.6 mm, the print width cab gives for 4 inch heads.
  `S l1;0,0,25,28,106` is refused with "Syntax error" on the display (and
  `B` in `ESC s`) although the head reports "106 mm"; 105.58 mm is
  accepted. [confirmed]
- The print head reports itself in the web interface as "106 mm 12 dots/mm"
  on the EOS2/300; Etiketto renders at exactly 12 dots per mm (8 for /200,
  23.6 for /600 models), so a 100 mm label is 1200 dots. Each print asks
  `qv` first and scales the image if it was rendered for another
  resolution. [confirmed for the /300]

## A print job

```
d PNG;ETIKETTO
<BASE64>
iVBORw0KGgo…              (the label as 1-bit PNG, one line of base 64)
</BASE64>
m m                       (millimetres)
J                         (job start)
S l1;0,0,25,28,50         (gap sensor; 0,0 offset; height 25, height + gap 28, width 50)
I:Label;0,0,0;ETIKETTO    (place the image at the top left, not rotated)
A 1                       (number of labels)
```

- `d PNG;name` with a base 64 block downloads an image into RAM; a name has
  at most 8 characters and a new download replaces the previous image of the
  same name. [manual]
- The image is the whole label at one pixel per dot: x across the print
  head, y along the feed. Etiketto turns it for content
  along the roll and dithers photos for the mono print head. [assumed:
  one pixel per dot, to be confirmed on a printout]
- `S l1;…` selects die-cut labels with gap detection for this job; `S e`
  would be continuous media, `S l2` reflective marks. [manual]
- No `H` (speed, heat, print method) and no `O` (options such as tear-off):
  the printer's setup stays in charge. [manual]
- `A [PREVIEW]` instead of `A 1` renders the label without printing; the
  result is shown at `http://<printer>/cgi-bin/bitmap` (behind the web
  interface's password). The job above was accepted that way without a
  protocol error. [confirmed]
- While a job runs, the label counter of `ESC s` counts down to 0 and the
  last character goes back to `N`. Etiketto counts a job as done once it saw
  labels pending and then none, or after 3 s without any activity.
  [manual] [open: how fast it counts on a real printout]
- On an error during a job (out of labels, head open, timeout) Etiketto
  sends `ESC c` (cancel job), so that the remaining labels are not printed
  once the problem is solved and the user prints again. [manual]
- Peel-off and print on demand (the printer waits for a button or a removed
  label) are not supported: the job would time out after 180 s.

## Not used yet

- Continuous media (`S e`), cutter (`C` command, `qp` → `CUTTER`),
  peel-off and applicators.
- Printers with a wider head (SQUIX 6, MACH 6?) or two heads (XC, XD).
- Printer-side fonts, barcodes and text (`T`, `B`): Etiketto sends the
  rendered label as one image, so the preview stays exact.
