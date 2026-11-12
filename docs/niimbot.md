# NIIMBOT label printers: USB serial protocol

How Etiketto talks to NIIMBOT label printers (B1, B21, B3S, B4, D11, D110,
D101 and related models). NIIMBOT does not publish the protocol. Etiketto's
implementation is written anew from protocol notes taken from two open
source projects:

- [niimbluelib](https://github.com/MultiMote/niimbluelib) by MultiMote
  (MIT, commit `35110a7`): packets, commands, print sequences, image
  encoding, and the model table generated from NIIMBOT's own `devices.json`.
- [Thermalith](https://github.com/EvilGeniusLabs-ca/Thermalith)
  (`src/Niimbot.Net`) by EvilGeniusLabs (GPL-3.0-or-later, commit
  `4c43cab`): USB serial on Linux, the probe, splitting long row runs, and
  the models it has tried on hardware (B1, B4, D11 H, B3S P).

No code of either project is included. **Etiketto has not been tried on a
NIIMBOT printer yet.**

Markers: **[spec]** in the sources above, **[assumed]** likely but not in
the sources and untested, **[open]** unknown.

## Transport

- USB: the printer is a CDC ACM device, `/dev/ttyACMn` on Linux. 115200
  baud, 8N1, no flow control, DTR and RTS asserted. [spec]
- Etiketto sets the port raw with termios, asserts DTR and RTS with
  `TIOCMBIS` and holds it exclusively (`TIOCEXCL`) while a session runs.
  Standard library only, no pyserial. [assumed: exclusive mode keeps
  ModemManager and other programs away during a job]
- Permissions: the node belongs to `dialout` (Fedora, Debian, Ubuntu) or
  `uucp` (Arch). Without the group Etiketto says which one to join
  (`serial_permission`). [spec]
- Flatpak: `--device=all` exposes `/dev/ttyACM*` and `/dev/serial/by-id`;
  sysfs (`/sys/class`, `/sys/devices`) is always visible in the sandbox.
  Serial ports have no portal. [assumed: not tried in the Flatpak]
- ModemManager may probe a new port for a modem right after it appears.
  If it gets in the way, a udev rule with `ENV{ID_MM_DEVICE_IGNORE}="1"`
  for the printer's IDs helps. [assumed]
- Bluetooth (BLE GATT service `e7810a71-73ae-499d-8c15-faa9aef0c3f2`, and
  RFCOMM on some models) carries the same packets. **Not implemented**;
  planned once Etiketto's interface has a place for Bluetooth printers.
  [spec for BLE]

### Finding the printer

- `discover()` lists `/sys/class/tty/ttyACM*`; `device/..` of each is the
  USB device with `idVendor`, `idProduct`, `manufacturer`, `product` and
  `serial`. A port counts as a NIIMBOT printer if "NIIMBOT" appears in the
  manufacturer or product string, or the vendor id is 0x3513. Nothing is
  sent to a port to find out. [assumed: neither the strings nor the vendor
  id are in the sources; to be checked with `lsusb` and
  `udevadm info /dev/ttyACM0`]
- The address is the stable `/dev/serial/by-id/...` link if udev made one,
  else `/dev/ttyACMn`. Any device path, or a bare name like `ttyACM0`, can
  be entered by hand. A host name is refused (UsbNotFound).

## Packets [spec]

```
55 55  CMD  LEN  DATA[LEN]  CHK  AA AA
```

- `LEN` is the number of data bytes (0 to 255), `CHK` the XOR of `CMD`,
  `LEN` and every data byte. Numbers are big endian.
- Requests without data carry one byte `01`.
- Examples: heartbeat `55 55 DC 01 02 DF AA AA`, density 3
  `55 55 21 01 03 23 AA AA`, model id `55 55 40 01 08 49 AA AA`.
- Answers have their own ids (below). `0xDB` (one byte: error code) or
  `0x00` (not supported) can come instead of the expected answer.
- Reads are a byte stream: several packets in one read, a packet split
  over reads. Etiketto re-assembles them; bytes that are no valid packet
  (wrong checksum or tail) are skipped one at a time. A header followed by
  a length that never completes is dropped as soon as a complete packet
  follows it.
- Requests are sent one at a time. Answers nobody waits for are dropped,
  except `0xDB` with an error, which ends the request.
- Connect (`0xC1`) is sent without the `03` prefix one comment in
  niimbluelib mentions for some firmware. [assumed]

| Request | Name | Data | Answer |
|---|---|---|---|
| `C1` | Connect | `01` | `C2`: 1 connected, 3 connected (protocol 3+) |
| `A5` | Printer status data | `01` | `B5`: protocol from bytes 11 and 12 |
| `40` | Printer info | type: 8 model id, 9 software version | `40 + type` |
| `DC` | Heartbeat | type: 1, 2 (basic), 3, 4 | `DD`, `DE`, `DF`, `D9` |
| `21` | Set density | density | `31` |
| `23` | Set label type | 1 gaps, 2 black mark, 3 continuous, 5 transparent | `33` |
| `01` | Print start | 1, 7 or 9 bytes, by sequence | `02` |
| `20` | Print clear | `01` | `30` |
| `03` | Page start | `01` | `04` |
| `13` | Set page size | 4, 6 or 13 bytes, by sequence | `14` |
| `15` | Print quantity | copies (u16) | `16` |
| `E3` | Page end | `01` | `E4` |
| `A3` | Print status | `01` | `B3`: labels printed (u16), …, byte 6 error |
| `F3` | Print end | `01` | `F4`: `01` = done |
| `DA` | Cancel | `01` | `D0` |

- Model id: `data[0] << 8 | data[1]`. [spec]
- Protocol: `n = data[11] * 100 + data[12]` of the status data; 204 to 299
  is protocol 3, 300 and 301 protocol 4, 302 and up protocol 5. [spec]
- Software version: a 16 bit number in hundredths, shown as `5.14`.
  [assumed: the format; only for the details dialog]
- The heartbeat answer's fields (lid, battery, paper; their offsets depend
  on the answer's length and the model) are not read: the sources do not
  agree on them. Etiketto sends a basic heartbeat in the status query only
  to see whether the printer reports an error. [open]
- Paper info (`59` → `69`: gap, label length, width) is not used, so
  Etiketto shows no measured label distance. [open: whether "total" is the
  label or label plus gap]

## Rows [spec]

- A row is the line of pixels across the print head; rows go in feed
  direction. Each row has `ceil(width / 8)` bytes, the leftmost pixel in
  the highest bit, 1 = black.
- Blank row: `84` `[row u16, repeat]`.
- Up to six black pixels: `83` `[row u16, c1, c2, c3, repeat, x u16 …]`,
  the pixel positions counted from the left.
- Otherwise: `85` `[row u16, c1, c2, c3, repeat, row bytes]`.
- `repeat`: identical rows that follow, including this one. One byte:
  runs longer than 255 are split into several packets at advancing row
  numbers (Thermalith; niimbluelib lets the byte wrap).
- `c1 c2 c3`: black pixels per third of the head (`head px // 24` bytes
  each) if the row fits into the three thirds, else the total as
  `[0, low byte, high byte]`. B21 (protocol 1) always uses the total.
  A third with more than 255 black pixels is sent as 255. [assumed: the
  sources do not say; clamping is safer than wrapping]
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
- Resolution: 203 dpi models are rendered at exactly 8 dots per mm, the
  300 dpi models at 300 dpi. The resolution is never derived from the
  printer's mm-per-dot value (no NIIMBOT printer has 229 dpi). [spec]
- A label wider than the head (a 50 mm label on the 48 mm head of a B1) is
  cut to the middle of the head's width; a label wider than the model's
  widest label is refused (LabelTooWide). The widest labels are 50 mm for
  the B1/B21 family, 15 mm for D11/D110, 25 mm for D101, 75 mm for B3S and
  the head width rounded up + 2 mm for the others (B4: 106 mm). [assumed]
- A label narrower than the head is sent as it is; the printer places it
  (centred on the B1 and B21?). [open]
- At most 65535 rows (the row number is 16 bits).

## Print sequences [spec]

The model id picks the sequence. All jobs start with density and label type.

| Sequence | Models | Start | Per label | Done when |
|---|---|---|---|---|
| B1 | B1, B1 SE, B3S, B3S P, B4, D101, B21 C2B, B21 L2B, D110 M (old firmware), B203, B3, B31, B32, B18, H1S | `01` 7 bytes: copies u16, 0, 0, 0, 0, colour 0 | `03`, `13` 6 bytes (rows, columns, copies), rows, `E3` | `A3` polled until the page count reaches the copies; then `F3` |
| D110 | D11, D110, B21S | `01` `[1]` | `20`, `03`, `13` 4 bytes, `15` copies, rows, `E3` | as B1 |
| B21 (protocol 1) | B21 | `01` `[1]` | per copy: `03`, `13` 4 bytes, rows with a check line `86 [row u16, 1]` after every 200 rows, `E3` | `F3` polled until it answers `01` |
| Protocol 4 | B1 PRO, B21 PRO, D11 H, D110 M (protocol 4) | label type before density; `01` 9 bytes: copies u16, 0, 0, 0, 0, colour 0, speed 1, 0; then `A3` without waiting | `03`, `13` 13 bytes (rows, columns, copies, then 7 zero bytes: cut height, cut type, 0, send all, part height), rows, `E3` | as B1; after `F3` a heartbeat `DC 01` without waiting |

- The models from B203 on in the B1 row have no sequence of their own in
  the sources; Thermalith keeps them on the B1 sequence. [assumed]
- Protocol 4: the meaning of the last byte of print start and of the zero
  bytes in the page size is not in the sources. [assumed]
- The check line's row number: the row after which it is sent (200, 400,
  …). [assumed]
- Timeouts: 1 s per answer, 5 s per status poll, 10 s for the answer after
  the rows of a page, a poll every 0.3 s; a job fails when no label came
  out for 60 s. [spec, the last one assumed]
- Copies: up to 65535 per job; Etiketto reports each label as it comes out
  (from the page count). A job that fails says how many labels were
  already printed.
- After a failure Etiketto sends Cancel (`DA`). [assumed]

## Errors [spec]

Codes in `0xDB` answers and in byte 6 of the print status:

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
`devices.json`. Only these models are known to Etiketto; another model id
is shown as "model N" and printing on it is refused.

| Model | Ids | dpi | Head px | Label types | Density |
|---|---|---|---|---|---|
| B1 | 4096 | 203 | 384 | gap, black mark, transparent | 1 to 5, 3 |
| B1 PRO | 4097 | 300 | 567 | gap, black mark, transparent | 1 to 5, 3 |
| B1 SE | 4098 | 203 | 384 | gap, black mark, transparent | 1 to 5, 3 |
| B21 | 768 | 203 | 384 | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B21 PRO | 785 | 300 | 591 | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B21 C2B | 771, 775 | 203 | 384 | gap, continuous, transparent, black mark | 1 to 5, 3 |
| B21 L2B | 769 | 203 | 384 | gap, black mark, transparent | 1 to 5, 3 |
| B21S | 777 | 203 | 384 | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B203 | 2816 | 203 | 400 | gap, black mark, transparent | 1 to 5, 3 |
| B3 | 52993 | 203 | 600 | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B31 | 5632 | 203 | 600 | gap, black mark, transparent | 1 to 5, 3 |
| B32 | 2049 | 300 | 851 | gap, transparent | 1 to 15, 10 |
| B3S | 256, 260, 262 | 203 | 576 | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B3S P | 272 | 203 | 576 | gap, black mark, continuous, transparent | 1 to 5, 3 |
| B4 | 6656 | 203 | 832 | gap, black mark, transparent | 1 to 5, 3 |
| D11 | 512 | 203 | 96 | gap, transparent | 1 to 3, 2 |
| D11 H | 528 | 300 | 142 | gap, transparent | 1 to 5, 3 |
| D110 | 2304, 2305 | 203 | 96 | gap, transparent | 1 to 3, 2 |
| D110 M | 2320 | 203 | 96 | gap, transparent | 1 to 5, 3 |
| D101 | 2560 | 203 | 192 | gap, transparent | 1 to 3, 2 |
| B18 | 3584 | 203 | 96 | gap, transparent, continuous | 1 to 3, 2 |
| H1S | 4352 | 203 | 96 | gap, continuous, transparent | 1 to 3, 2 |

The print dialog offers the density range and label types of the connected
model ("Print density", "Label type"); a saved choice the model does
not take falls back to its default. B18's black-mark-gap and heat-shrink
tube media are not offered.

## To check on a printer

1. `lsusb`, `udevadm info /dev/ttyACM0`: vendor id and USB strings, so that
   discovery finds the printer.
2. The query: model id, protocol, software version format.
3. Printing on a B1 (B1 sequence) and a D11 or D110 (D110 sequence):
   orientation and mirroring of "left" models, where a narrow label lands
   on the head, the pixel counts.
4. B21 (protocol 1) and a protocol 4 model (B1 PRO, D11 H).
5. Errors: cover open and no labels before and during a job.
