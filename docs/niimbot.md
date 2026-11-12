# NIIMBOT label printers: USB serial protocol

How Etiketto talks to NIIMBOT label printers (B1, B21, B3S, B4, D11, D110,
D101 and related models). NIIMBOT does not publish the protocol. Etiketto's
implementation is written anew from protocol notes taken from two open
source projects:

- [niimbluelib](https://github.com/MultiMote/niimbluelib) by MultiMote
  (MIT, commit `35110a7`): packets, commands, print sequences, image
  encoding, heartbeat decoding, and the model table generated from
  NIIMBOT's own `devices.json`.
- [Thermalith](https://github.com/EvilGeniusLabs-ca/Thermalith)
  (`src/Niimbot.Net`) by EvilGeniusLabs (GPL-3.0-or-later, commit
  `4c43cab`): USB serial on Linux, placing the label on the head, splitting
  long row runs, stock label widths (`printers.json`), and the models it
  has tried on hardware (B1, B4, D11 H, B3S P).

No code of either project is included. **Etiketto has not been tried on a
NIIMBOT printer yet.**

Markers: **[spec]** in the sources above, **[assumed]** likely but not in
the sources and untested, **[open]** unknown.

## Transport

- USB: the printer is a CDC ACM device, `/dev/ttyACMn` on Linux. 115200
  baud, 8N1, no flow control, DTR and RTS asserted. [spec]
- Etiketto sets the port raw with termios, asserts DTR and RTS with
  `TIOCMBIS` and opens it in exclusive mode (`TIOCEXCL`) while a session
  runs, so that a second `open()` by another program fails. Standard
  library only, no pyserial.
- Permissions: the node belongs to `dialout` (Fedora, Debian, Ubuntu) or
  `uucp` (Arch). Without the group Etiketto says which one to join
  (`serial_permission`). [spec]
- Flatpak: `--device=all` exposes `/dev/ttyACM*` and `/dev/serial/by-id`;
  sysfs (`/sys/class`, `/sys/devices`) is always visible in the sandbox.
  Serial ports have no portal. [assumed: not tried in the Flatpak]
- ModemManager may probe a new port for a modem right after it appears
  (exclusive mode does not stop a probe that comes first, or one by root).
  If it gets in the way, a udev rule keeps it off the printer:
  `ATTRS{idVendor}=="3513", ENV{ID_MM_DEVICE_IGNORE}="1"` in
  `/etc/udev/rules.d/77-niimbot.rules`. [assumed]
- Old B3S with micro-USB (ids 256, 260, 262) are silent on USB serial
  (and on SPP); only the current USB-C B3S P was seen answering. [spec:
  Thermalith] Etiketto keeps the model in its table for later Bluetooth
  support, but it cannot reach it over USB.
- Bluetooth (BLE GATT service `e7810a71-73ae-499d-8c15-faa9aef0c3f2`, and
  RFCOMM on some models) carries the same packets. **Not implemented**;
  planned once Etiketto's interface has a place for Bluetooth printers.
  [spec for BLE]

### Finding the printer

- `discover()` lists `/sys/class/tty/ttyACM*`; `device/..` of each is the
  USB device with `idVendor`, `idProduct`, `manufacturer`, `product` and
  `serial`. A port counts as a NIIMBOT printer if its vendor id is 0x3513
  [spec: Thermalith, B3S P with USB-C] or "NIIMBOT" appears in the
  manufacturer or product string [assumed]. Nothing is sent to a port to
  find out. Other models may use another vendor id [open: to be checked
  with `lsusb` and `udevadm info /dev/ttyACM0`].
- The address is the stable `/dev/serial/by-id/...` link if udev made one,
  else `/dev/ttyACMn`. A device path, or a bare name like `ttyACM0`, can be
  entered by hand. Only USB serial ports are taken: the name (after
  following links) must be `ttyACMn` or `ttyUSBn` and the file a character
  device; a `/dev/serial/...` link may also be missing (unplugged). Host
  names, pseudo terminals (`/dev/pts/n`) and built-in ports (`ttyS0`) are
  refused (UsbNotFound).
- A port that hangs up (the read returns nothing although it is readable)
  and whose device file is gone ends the session with UsbNotFound at once.

## Packets [spec]

```
55 55  CMD  LEN  DATA[LEN]  CHK  AA AA
```

- `LEN` is the number of data bytes (0 to 255), `CHK` the XOR of `CMD`,
  `LEN` and every data byte. Numbers are big endian.
- Requests without data carry one byte `01`.
- Connect (`C1`) gets one byte `03` in front of the whole packet:
  `03 55 55 C1 01 01 C1 AA AA`.
- Examples: heartbeat `55 55 DC 01 01 DC AA AA`, density 3
  `55 55 21 01 03 23 AA AA`, model id `55 55 40 01 08 49 AA AA`.
- Answers have their own ids (below). `0xDB` (one byte: error code) or
  `0x00` (not supported) can come instead of the expected answer.
- Reads are a byte stream: several packets in one read, a packet split
  over reads. Etiketto re-assembles them; bytes that are no valid packet
  (wrong checksum or tail) are skipped one at a time. A header followed by
  a length that never completes is dropped as soon as a complete packet
  follows it.
- Requests are sent one at a time. Answers nobody waits for are dropped,
  except `0xDB` with an error, which ends the request, and `0xE0` (page
  index, below), which is kept.

| Request | Name | Data | Answer |
|---|---|---|---|
| `C1` | Connect | `01` | `C2`: 1 connected, 2 connected (protocol 1), 3 connected (protocol 3+) |
| `A5` | Printer status data | `01` | `B5`: protocol from bytes 11 and 12 |
| `40` | Printer info | type: 8 model id, 9 software version | `40 + type` |
| `DC` | Heartbeat | type: 1, 2, 3, 4 | `DD`, `DE`, `DF`, `D9` |
| `21` | Set density | density | `31` |
| `23` | Set label type | 1 gaps, 2 black mark, 3 continuous, 5 transparent | `33` |
| `01` | Print start | 1, 2, 7 or 9 bytes, by sequence | `02` |
| `20` | Print clear | `01` | `30` |
| `03` | Page start | `01` | `04`: `01` accepted |
| `13` | Set page size | 2, 4, 6, 9 or 13 bytes, by sequence | `14` |
| `15` | Print quantity | copies (u16) | `16` |
| `86` | Check line | row (u16), `01` | `D3` |
| `E3` | Page end | `01` | `E4`: `01` accepted |
| `A3` | Print status | `01` | `B3` |
| `F3` | Print end | `01` | `F4`: `01` = done |
| `DA` | Cancel | `01` | `D0` |

- Model id: `data[0] << 8 | data[1]`. [spec]
- Protocol: Connect answer 2 means protocol 1. Answer 3: the status data
  tell it, `n = data[11] * 100 + data[12]`; 204 to 299 is protocol 3, 300
  and 301 protocol 4, 302 and up protocol 5. [spec] Nothing in the sources
  reports protocol 2. [open]
- Software version: two bytes, a number in hundredths ("7.81" on the
  B3S P). [spec: Thermalith; niimbluelib is not sure of the format] Only
  shown in the details dialog.
- Print status: labels printed (u16), print progress and feed progress of
  the current label (percent); only the 10 byte form has an error code, in
  byte 6. [spec]

### Heartbeat [spec]

The status query sends heartbeat type 4 (`D9`) from protocol 3 on, else
type 1 (`DD`), as niimbluelib and Thermalith do. Bytes 0 = closed or
inserted:

| Answer | Length | Lid | Battery | Paper |
|---|---|---|---|---|
| `DD` | 10 (D110) | 8 | 9 | - |
| `DD` | 13 (B1) | 9 | 10 | 11 |
| `DD` | 19 | 15 | 16 | 17 |
| `DD` | 20 | - | - | 18 |
| `D9` | 9 or more | 4 | 2 | 5 |

- These models report the lid the other way round: 512, 513, 514, 1792,
  2304, 2560, 3584, 3840, 4352, 5120, 272, 273, 274. [spec]
- Battery: 0 to 4 are steps of 25 %, larger values percent. [spec]
- Etiketto shows an open lid as "cover open", missing paper as "out of
  labels" and a charge of 10 % or less as "battery low" in the status row.
  A charge of 0 is ignored: printers without a battery may report it.
  [assumed: the threshold and the meaning of 0]
- Other lengths, or no answer, tell nothing; the query does not fail.
- Paper info (`59` → `69`: gap, label length, width) is not used, so
  Etiketto shows no measured label distance. [open]

## Rows [spec]

- A row is the line of pixels across the print head; rows go in feed
  direction. Each row has `ceil(head px / 8)` bytes, the leftmost pixel in
  the highest bit, 1 = black.
- Blank row: `84` `[row u16, repeat]`.
- Up to six black pixels: `83` `[row u16, c1, c2, c3, repeat, x u16 …]`,
  the pixel positions counted from the left.
- Otherwise: `85` `[row u16, c1, c2, c3, repeat, row bytes]`.
- `repeat`: identical rows that follow, including this one. Runs end at
  each check point, after rows 199, 399, … (niimbluelib's encoder does
  this for every model), so they never reach the one-byte limit; a run
  longer than 255 would also be split at advancing row numbers
  (Thermalith).
- `c1 c2 c3`: black pixels per third of the head (`head px // 24` bytes
  each) if the row fits into the three thirds, else the total as
  `[0, low byte, high byte]`. B21 and B21 L2B always use the total. A
  third with more than 255 black pixels wraps to one byte, as in
  niimbluelib.
- B21 and B21 L2B send a check line `86 [row u16, 01]` after rows 199,
  399, … and wait for its answer `D3` before the next rows (a `C6` "reset
  timeout" may come first and is dropped).
- Rows get no answer.
- Example: `5555 83 0e 007e 000400 01 0027 0028 0029 002a fa aaaa` is row
  126 with black pixels 39 to 42.

### Geometry

- Etiketto renders die-cut labels in printer orientation: x across the
  head, y along the feed. That is what the printer takes for both of
  NIIMBOT's print directions: "top" models (B1, B21, B3S, B4) take the
  image as it is; for "left" models (D11, D110, D101) the NIIMBOT app turns
  its landscape canvas 90 degrees clockwise first, which is what
  "Content direction: Along the roll" already does. [spec for the
  directions; assumed: no mirroring, to be checked on a D11]
- Every page is exactly as wide as the head; the page size gives the
  head's width as columns. The protocol has no horizontal offset, so a
  narrower label is padded with white on both sides and centred; a wider
  one (a 50 mm label on the 48 mm head of a B1) is cut to the middle.
  [spec: Thermalith `PositionOnHead`, centred by default]
- A label wider than the model's widest stock label is refused
  (LabelTooWide): 50 mm for the B1/B21 family (54 mm B1 SE), 15 mm for
  D11/D110/H1S/B18, 25 mm for D101, 75 to 77 mm for the B3 series, 108 mm
  for the B4. [spec: Thermalith `printers.json`, `stockWidthMm`]
- Resolution: 203 dpi models are rendered at exactly 8 dots per mm, the
  300 dpi models at 300 dpi. The resolution is never derived from the
  printer's mm-per-dot value (no NIIMBOT printer has 229 dpi). [spec]
- At most 65535 rows (the row number is 16 bits).

## Print sequences [spec]

The model id and the protocol pick the sequence (niimbluelib
`modelPrintTasks`). All jobs start with density and label type (protocol 4:
label type first).

| Sequence | Models | Start | Per label | Done when |
|---|---|---|---|---|
| B1 | B1, B21 C2B, D101, D110 M (except protocol 4); B3S P and B4 on any protocol | `01` 7 bytes: copies u16, 0, 0, 0, 0, colour 0 | `03`, `13` 6 bytes (rows, columns, copies), rows, `E3` | status polled |
| D110 | D110, B21S, D11 on protocol 1 or 2 | `01` `[1]` | `20`, `03`, `13` 4 bytes (rows, columns), `15` copies, rows, `E3` | status polled |
| Old D11 | D11S, D11 (except protocol 1 and 2) | `01` `[1]` | `20`, `03`, `13` 2 bytes (rows), `15` copies, rows, `E3` | the printer sends `E0` with the number of each label printed |
| B21 | B21 | `01` `[1]` | per copy: `03`, `13` 4 bytes, rows with check lines, `E3` | `F3` polled until it answers `01` |
| B21 L2B | B21 L2B | `01` `[1]` | per copy: `03` repeated until it answers `01` (5 tries; 0.5 s after a refusal, at once after a timeout of 1 s or an error answer), `13` 4 bytes, rows with check lines, `E3` | `E3` repeated the same way until it answers `01` (20 tries) |
| H1S | H1S | `01` 2 bytes: copies | `03`, `13` 9 bytes (rows, columns, copies, cut height 0, cut type 0), rows, `E3` | status polled |
| Protocol 4 | B1 PRO, B21 PRO, D11 H, D110 M on protocol 4, B1 SE and the others without a sequence of their own on protocol 4 and up | `01` 9 bytes: copies u16, 0, 0, 0, 0, colour 0, speed 1, flag 0 | no page start: `A3` without waiting, `13` 13 bytes (rows, columns, copies, cut height 0, cut type 0, 0, send all 0, part height 0), rows, `E3` | status polled; after `F3` a heartbeat `DC 01` without waiting |

- B3S P and B4 take the B1 sequence whatever protocol they report: tried
  on hardware by Thermalith (`KnownPrinterFacts.VerifiedTasks`). [spec]
- Models without a sequence of their own in niimbluelib (B1 SE, B203, B3,
  B31, B32, B3S, B18) take the protocol 4 sequence from protocol 4 on,
  else the B1 sequence, as Thermalith does. [spec, the B1 fallback from
  Thermalith]
- Protocol 4: the status request after the print start and the heartbeat
  after the print end are sent without waiting because the B21 PRO drops
  the first packet after each; the test printer does the same. [spec]
- Only the D11 switches to the D110 sequence on protocol 1 and 2; the D11S
  always takes the old D11 sequence. [spec]
- Status polled: `A3` every 0.3 s until the page count reaches the copies
  and the last label is printed to 100 % and fed out to 100 % (protocol 4:
  printed only, its feed progress may stay below 100). [spec: Thermalith;
  niimbluelib only checks the page count] The first answer may still be
  the last job's: a finished status counts once the job was seen
  unfinished, or when the next poll says the same. [assumed]
- Then `F3`. After a good print an answer to `F3` that comes late or not at
  all is not an error. [assumed: Etiketto's choice, as the labels are out;
  niimbluelib and Thermalith (after 2 s) fail then]
- Timeouts: 1 s per answer, 5 s per status poll and between two `E0`, 10 s
  for the answer after the rows of a page and for a check line; a job fails
  when the status did not change for 60 s. The B21's `F3` answers carry no
  progress: there a job may take 60 s per label. [spec, the 60 s assumed]
- Copies: up to 65535 per job; Etiketto reports each label as it comes
  out. A job that fails says how many labels were already printed.
- After a failure Etiketto sends Cancel (`DA`). [assumed]

## Errors [spec]

Codes in `0xDB` answers and in byte 6 of the 10 byte print status:

| Code | Meaning | Etiketto |
|---|---|---|
| 1 | cover open | CoverOpen |
| 2, 8 | no paper | OutOfLabels |
| 3, 4 | low battery, battery problem | LowBattery |
| 5 | canceled at the printer | JobCanceled |
| 9 | busy | PrinterBusy |
| 10, 12 | no print head, print head loose | HeadOpen |
| 13, 14, 15 | ribbon missing, wrong or used up | RibbonError |
| 27 | abnormal paper output (B3S) | PaperJam |
| 28 | check the paper | LabelNotFound |
| others | data error, overheated, wrong paper, … | PrinterError with the code |
| 20, 29, 34 | RFID tag not read or written | ignored: the printer prints anyway |

In the status query an error answer is shown as the printer's state
("needs attention"), not as a failure.

## Models [spec]

From niimbluelib's `printer_models.ts`, generated from NIIMBOT's
`devices.json`; widest label from Thermalith's `printers.json`. Only these
models are known to Etiketto; another model id is shown as "model N" and
printing on it is refused.

| Model | Ids | dpi | Head px | Widest label | Label types | Density |
|---|---|---|---|---|---|---|
| B1 | 4096 | 203 | 384 | 50 mm | gap, black mark, transparent | 1 to 5, 3 |
| B1 PRO | 4097 | 300 | 567 | 50 mm | gap, black mark, transparent | 1 to 5, 3 |
| B1 SE | 4098 | 203 | 384 | 54 mm | gap, black mark, transparent | 1 to 5, 3 |
| B21 | 768 | 203 | 384 | 50 mm | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B21 PRO | 785 | 300 | 591 | 50 mm | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B21 C2B | 771, 775 | 203 | 384 | 50 mm | gap, continuous, transparent, black mark | 1 to 5, 3 |
| B21 L2B | 769 | 203 | 384 | 50 mm | gap, black mark, transparent | 1 to 5, 3 |
| B21S | 777 | 203 | 384 | 50 mm | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B203 | 2816 | 203 | 400 | 50 mm | gap, black mark, transparent | 1 to 5, 3 |
| B3 | 52993 | 203 | 600 | 75 mm | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B31 | 5632 | 203 | 600 | 77 mm | gap, black mark, transparent | 1 to 5, 3 |
| B32 | 2049 | 300 | 851 | 75 mm | gap, transparent | 1 to 15, 10 |
| B3S (not on USB) | 256, 260, 262 | 203 | 576 | 75 mm | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B3S P | 272 | 203 | 576 | 75 mm | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B4 | 6656 | 203 | 832 | 108 mm | gap, black mark, transparent | 1 to 5, 3 |
| D11 | 512 | 203 | 96 | 15 mm | gap, transparent | 1 to 3, 2 |
| D11S | 514 | 203 | 96 | 15 mm | gap, transparent | 1 to 3, 2 |
| D11 H | 528 | 300 | 142 | 15 mm | gap, transparent | 1 to 5, 3 |
| D110 | 2304, 2305 | 203 | 96 | 15 mm | gap, transparent | 1 to 3, 2 |
| D110 M | 2320 | 203 | 96 | 15 mm | gap, transparent | 1 to 5, 3 |
| D101 | 2560 | 203 | 192 | 25 mm | gap, transparent | 1 to 3, 2 |
| B18 | 3584 | 203 | 96 | 15 mm | gap, transparent, continuous | 1 to 3, 2 |
| H1S | 4352 | 203 | 96 | 15 mm | gap, continuous, transparent | 1 to 3, 2 |

The print dialog offers the density range and label types of the connected
model ("Print density", "Label type"). Both are saved once per printer
family, not per model: a saved density outside the connected model's range
is clamped into it (5 on a D11 becomes 3), a label type the model does not
take falls back to its default. B18's black-mark-gap and heat-shrink tube
media are not offered.

## To check on a printer

1. `lsusb`, `udevadm info /dev/ttyACM0`: vendor id and USB strings of
   models other than the B3S P, so that discovery finds them.
2. The query: model id, protocol, heartbeat (lid, paper, battery) and the
   software version format.
3. Printing on a B1 (B1 sequence) and a D11 or D110 (old D11 and D110
   sequences): orientation and mirroring of "left" models, a narrow label
   centred on the head.
4. B21 and B21 L2B (check lines), H1S, and a protocol 4 model (B1 PRO,
   D11 H).
5. Errors: cover open and no labels before and during a job.
