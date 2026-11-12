# cab printers: JScript over the network

How Etiketto talks to cab label printers with a 4 inch print head (EOS2,
EOS5, SQUIX 4, MACH 4S). JScript is cab's own printer language and is
publicly documented by cab: "JScript Programming Manual" for SQUIX, MACH 4S,
EOS2, EOS5, XC Q, XD Q, HERMES Q, PX Q, AXON 1/2, linked from
<https://www.cab.de/en/programming>. Nothing here is reverse engineered;
the device answers below were seen on an EOS2/300 with firmware
5.47.7 (Jan 22, 2026).

Sources quoted as **[manual]**:

- "JScript Programming Manual", edition 07/2026, firmware 5.48
  (<https://www.cab.de/media/pushfile.cfm?file=3047>): ESC commands
  (2.x), `q` queries (3.x), `H` (4.8), `O` (4.12), `S` (4.15), `A [PREVIEW]`
  (4.1).
- "Programming a cab Printer" (JScript guide), edition 04/2022
  (<https://www.cab.de/media/pushfile.cfm?file=3963>): 4.2.4 "Speed and
  heat", 9.4 "Ribbonsaver", tables 14.2 (`H`) and 14.3 (`O`).

Markers: **[manual]** from cab's manual, **[confirmed]** seen on the device,
**[assumed]** likely but untested, **[open]** unknown.

## Transport

- TCP port 9100 ("RawIP"), with the interpreter for RawIP set to JScript
  (the default; the printer can also run ZPL there). [confirmed]
- USB 2.0 (device port): Linux's printer class driver (usblp) makes it
  `/dev/usb/lpN`, which takes the same JScript and answers the ESC
  commands, as the driver is bidirectional. Etiketto finds cab printers
  there by the IEEE 1284 device ID in sysfs (`MFG:cab`) and addresses them
  like CUPS: `usb://cab/EOS2%2F300?serial=...`. [manual] [assumed: not yet
  tried on a printer]
- Timeouts on USB: 10 s for an answer; while sending a job, 10 s without
  the printer taking any data (each write that gets data through starts it
  again), at most 10 s + the job's size at 100 kB/s. A print waits up to
  45 s for a status query still using the device, a status query 5 s. On
  TCP the socket's timeout of 10 s applies as before.
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
  found), `F` out of ribbon, `R` ribbon in thermal direct mode (see "Print
  method"). Others (`B`
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
H 50,0,T                  (only with a print method chosen in Etiketto, see below)
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
- `H` only if a print method is chosen (next section); no `O` (options
  such as tear-off, backfeed): the printer's setup stays in charge of
  those. [manual]
- `A [PREVIEW]` instead of `A 1` renders the label without printing; the
  result is shown at `http://<printer>/cgi-bin/bitmap` (behind the web
  interface's password). The job above was accepted that way without a
  protocol error. [confirmed]
- While a job runs, the label counter of `ESC s` counts down to 0 and the
  last character goes back to `N`. Etiketto counts a job as done once it saw
  labels pending and then none, or after 3 s without any activity.
  [manual] [open: how fast it counts on a real printout]
- On an error while sending or during a job (out of labels, head open,
  timeout) Etiketto sends `ESC c` (cancel job), so that the remaining
  labels are not printed once the problem is solved and the user prints
  again. [manual] After a job cut off while sending, it should also drop
  the half-downloaded image, which on USB would otherwise run into the next
  job. [assumed: not tried on a printer; a printer that stalled may not
  take `ESC c` at all, so Etiketto tries it for 1 s only and then closes]
- Peel-off and print on demand (the printer waits for a button or a removed
  label) are not supported: the job would time out after 180 s.

## Print method (thermal transfer or thermal direct)

The printer prints with a ribbon (thermal transfer) or without (thermal
direct), as set in its setup (Setup > Ribbon > Transfer print, `On`/`Off`).
A thermal transfer label printed in thermal direct mode comes out blank or
smeared, and the ribbon is not pulled. Seen on an EOS2/300 whose setup said
"Transfer print: Off" while a ribbon and thermal transfer labels were
loaded: the counters of the web interface showed 52.23 m thermal transfer
and 0.156 m thermal direct, the Windows driver printed fine (it sends the
method with every job), Etiketto's job did not pull the ribbon.
[confirmed]

### The `H` command [manual]

`H speed[,h[:h]][,P[x]][,t[:t]][,r][,s][,Bb]` (manual 4.8), sets for the
current job:

| Field | Meaning |
|---|---|
| `speed` | print speed in mm/s (inch/s with `m i`); **required**. A value the printer does not have is rounded to the next one it has, a value above its maximum becomes the maximum. |
| `h` | heat, -20 to +20; added to the heat level set on the printer ("the value for the heat is added to the default value in the printer", guide 4.2.4 and 9.4), so `0` keeps it. |
| `t` | `T` thermal transfer (with ribbon), `D` thermal direct (without); not given: the setup's value (guide table 14.2). `TI`/`TO` (ribbon ink side) are not available on EOS2/5 and MACH 4S. |
| `r` | `R0`/`R1` ribbon saver, only with that option fitted. |
| `Bb` | backfeed speed; 100 mm/s if not given. |
| `P[x]`, `:h`, `:t`, `s` | paper saving and the second head: X4 printers with two heads (XC Q, XD Q) only. |

Examples in the manual: `H 100,0,T`, `H 150,-5,T`, `H 150,0,D,R1,B75`,
`H 100,S1`; `H` comes after `J` and before `S`.

### What Etiketto sends

Setting "Print method" (`method` in settings.json, per printer model):

| Choice | Value | Job |
|---|---|---|
| As set on the printer (default) | `printer` | no `H` line: as before |
| Thermal transfer (ribbon) | `transfer` | `H <speed>,0,T` |
| Thermal direct | `direct` | `H <speed>,0,D` |

- Speed: `H` cannot leave it out. Etiketto sends the speed set on the
  printer, read from the web interface's setup
  (`GET /cgi-bin/get?cmd=1`, entry `ID_PRINT_SPEED`, e.g. `"50 mm/s"`)
  just before the job. If the web interface does not answer within 2 s
  (switched off, behind a password, USB) or shows inch/s, it sends 50 mm/s,
  the default of cab's setup and the slowest common speed (slower prints
  cleaner, guide 4.2.4). Trade-off: then a printer set to a faster
  speed prints slower than set, one set to 30 or 40 mm/s faster. [assumed:
  50 mm/s as cab's factory default, from the configuration manuals of
  other cab models; the EOS2's own manual gives no number]
- Heat: `0`, which adds nothing to the printer's heat level.
- Default "As set on the printer": a printer used for thermal direct
  labels without a ribbon keeps working as before; choosing thermal
  transfer by default would stop it with "out of ribbon" (`F`), and the
  speed would always be sent. The choice is kept per printer model, so it
  is made once.
- `--print-method printer|transfer|direct` on the command line overrides
  the app's choice for one print, also together with `--printer-defaults`
  (whose default is `printer`).
- Accepted by an EOS2/300 (firmware 5.47.7) with `A [PREVIEW]`:
  `H 50,0,T` and `H 50,0,D` before `S l1;0,0,17,20,50`, `ESC s` stayed
  `Y-000000N`/`Y-000000Y` (no `B`), no label was moved. [confirmed]
  Whether the ribbon is really pulled with `T` and the setup on `Off`:
  [open: to be confirmed on a printout]

### Knowing the printer's method

- JScript has no query for the setup's print method: `q` (manual 3.x)
  reports memory, peripherals (`qp`), ribbon diameter (`qr`, `-1` if not
  measured), label roll diameter (`qw`) and firmware (`qv`); `qo`
  (statistics with `mm_transfer`/`mm_thermo` counters) is for X4 printers.
  [manual] [open: what `qr` answers on the EOS2 in thermal direct mode
  with a ribbon loaded; not asked yet]
- The web interface's setup tells it without a password on firmware 5.47:
  `ID_TRANSFER_PRINT`, a radio button with `"options": ["Off", "On"]`,
  `"values": ["false", "true"]` and the shown `"selection"`. Etiketto reads
  the value at the position of the selection (the texts follow the
  printer's language) and shows it in the printer details as "Print method
  set on the printer". Only a hint: the web interface may be off or behind
  a password, and the media loaded is not known. [confirmed]
- `/cgi-bin/get?cmd=3` (status) has the counters `thermalTransfer` and
  `thermalDirect` in metres; not used. [confirmed]
- `ESC s` error `R` "Ribbon detected in thermal direct mode" and `F` "Out
  of ribbon" [manual]: Etiketto reports `F` as "the printer finds no
  ribbon" (load one, or choose thermal direct) and `R` as "a ribbon is
  loaded, but the printer prints without it" (choose thermal transfer, or
  remove the ribbon), during a print and in the status. On the EOS2/300
  with a ribbon loaded and "Transfer print: Off", `ESC s` said
  `Y-000000N` at rest, and during the print described above Etiketto's
  status polls reported no error either. [confirmed] So `R` cannot be
  relied on for this mismatch; the printer seems not to notice a ribbon it
  does not drive. [assumed] `ESC z` position E "ribbon warning" is
  hardware dependent. [manual]

## Label start, tear-off and backfeed

A print on the EOS2/300 that started in the gap or on the previous label
and used up to three labels for two one-label jobs came from a
hand-written test file, not from Etiketto or the printer's setup: with
the orientation "across", `length_mm` is the width across the head and
`tape_mm` the height along the feed (`Label.media_size`). The file had
`tape_mm` 40 and `length_mm` 17, so the job said `S l1;0,0,40,43,17` (40 mm
high, a label every 43 mm) on a roll of 50 × 20 mm labels; the corrected
file sends `S l1;0,0,18,20,48`. [confirmed] The window warns before
printing when the distance Etiketto sends (height + gap) differs from the
one `ESC l` reports ("Different labels loaded"); `etiketto print` says
the same on standard error and prints anyway.

What the manual says about the settings around the start of a label, for
later:

- Tear-off mode (`O T[x]` per job, setup "Tear-off mode"): after the job
  the last label is fed forward to the tear-off edge; the next job pulls
  it back first. [manual]
- Backfeed (setup "Backfeed": "optimized" or "always"): `O D` forces
  "always" for a job, which "feeds the label back and starts printing at
  the label margin"; `O P` is the "smart" mode that suppresses the backfeed
  ("a small horizontal white line" may appear where the label stopped
  under the head). [manual] Etiketto sends neither: the setup decides.
- `O` must directly follow `S`. [manual]
- `H` sets speed, heat, method, ribbon saver and backfeed speed; it does
  not change where a label starts. [manual]
- A printer that is not synchronised (`ESC l` → `N…`) feeds labels until
  it finds a gap before printing. [manual]

## Not used yet

- Continuous media (`S e`), cutter (`C` command, `qp` → `CUTTER`),
  peel-off and applicators.
- Printers with a wider head (SQUIX 6, MACH 6?) or two heads (XC, XD).
- Printer-side fonts, barcodes and text (`T`, `B`): Etiketto sends the
  rendered label as one image, so the preview stays exact.
