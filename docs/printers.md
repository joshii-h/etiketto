# Supported printers

What Tavletta does with each printer family, and how it is connected.
Protocol notes for each family are linked in the last column; all
documents are listed in [the index](README.md).

| Printer | Labels | What Tavletta does with it | Protocol |
|---|---|---|---|
| Brother VC-500W (ColAura) | Colour on ZINK tape cassettes, 9 to 50 mm wide, any length | Detects the cassette, remaining tape and the cassette's age; vivid, normal or black-and-white colours; full, half or no cut; colour adjustment | Brother's native protocol on TCP port 9100 or USB ([vc500w.md](vc500w.md)) |
| Brother QL (QL-500 to QL-1115NWB), **not yet tried on a printer** | Black on DK continuous rolls (12 to 104 mm, any length) or die-cut DK labels; red and black on the QL-800 series with DK-22251 | Reads the loaded roll and the printer's problems; full cut, cut after the last label or no cut; 600 dpi on the models that have it | Brother's raster commands on TCP port 9100 or USB ([brother-ql.md](brother-ql.md)) |
| Brother P-touch (PT-P700, PT-P750W, PT-E550W, PT-P710BT, PT-P900 series, PT-D series, older PT models), **not yet tried on a printer** | Black on TZe tape 3.5 to 36 mm wide and HSe heat-shrink tube, any length | Detects the tape, its type and printable width; full, half or no cut, chain printing, high resolution, tape feed; explains Editor Lite mode | Brother's raster commands on USB or TCP port 9100 ([ptouch.md](ptouch.md)) |
| cab EOS2, EOS5, SQUIX 4, MACH 4S | Black on die-cut labels up to 105.6 mm wide, 203, 300 or 600 dpi, thermal direct or transfer | Reads the resolution and the measured label distance and warns if the roll does not match; print method (ribbon or thermal direct) as set on the printer or chosen per printer; upside down; names ribbon problems; cancels a job stopped by a problem | JScript on TCP port 9100 or USB ([cab.md](cab.md)) |
| NIIMBOT B1, B21, B3S, B4, D11, D110, D101, **not yet tried on a printer** | Black on die-cut labels, continuous rolls or labels with black marks, 203 or 300 dpi, up to 108 mm wide depending on the model | Reads the model and offers its print density and label types; reports each label and how many came out before a problem | NIIMBOT's packet protocol on USB serial ([niimbot.md](niimbot.md)) |
| System printer (CUPS, IPP) | Paper or labels in any size the printer takes: office printers, printers set up in CUPS (USB too), IPP Everywhere printers on the network | Lists the CUPS queues and the IPP printers on the network; prints the label as one page at exactly its size, or on label sheets (A4, US Letter, over 1700 sheets); explains paper, toner and cover problems | IPP to CUPS on this computer or straight to the printer ([system.md](system.md)) |
| Zebra and compatibles (ZPL), **not yet tried on a printer** | Black on die-cut labels, black marks or continuous media up to 104 mm wide, 203, 300 or 600 dpi | Reads model and resolution; reports paper out, head open, ribbon and pause; label sensor, tear-off, peel-off or cutter, darkness and speed | ZPL on TCP port 9100 or USB ([zpl.md](zpl.md)) |
| Zebra EPL2 printers (LP/TLP 2824, 2844 and others), **not yet tried on a printer** | Black on die-cut labels, black marks or continuous media up to 104 mm wide, 203 or 300 dpi | Label sensor and darkness; no status | EPL2 on TCP port 9100 or USB ([epl.md](epl.md)) |
| TSPL printers: TSC, Munbyn, iDPRT, Xprinter, Rollo and other 4 × 6 in shipping label printers, **not yet tried on a printer** | Black on die-cut labels, black marks or continuous media up to 108 mm wide, 203 or 300 dpi | Reports head open, paper and ribbon problems where the printer answers; label sensor, tear-off, peel-off or cutter, darkness and speed | TSPL on TCP port 9100 or USB ([tspl.md](tspl.md)) |
| Receipt printers with ESC/POS (Epson TM and compatibles), **not yet tried on a printer** | Black on 58 or 80 mm paper rolls, as long as the content | Reports cover open and paper end where the printer answers; full, partial or no cut | ESC/POS on TCP port 9100 or USB ([escpos.md](escpos.md)) |
| DYMO LabelWriter 300, 400 and 450 series, 4XL, **not yet tried on a printer** | Black on DYMO die-cut labels, 300 dpi | Reports no paper and paper jam where the printer answers; darkness | DYMO's raster commands on USB or TCP port 9100 ([dymo.md](dymo.md)) |

Connections: network (LAN or Wi-Fi) for all but NIIMBOT; USB too, through the
kernel's printer driver: cab printers, Brother QL and P-touch printers, the
VC-500W and the ZPL, EPL2, TSPL, ESC/POS and DYMO printers are found on USB
by themselves, and the system printer reaches every printer CUPS has, USB
included. On USB, the VC-500W and cab printers have not been tried on a
printer yet; the Brother QL, P-touch, NIIMBOT, ZPL, EPL2, TSPL, ESC/POS
and DYMO backends have not been tried on any printer at all, they follow the
documented protocols. Your user needs to be in the group `lp`
(`sudo usermod -aG lp $USER`, then log in again). The Flatpak has
`--device=all` for this: `/dev/usb/lp*` is not covered by `--device=usb`.
NIIMBOT printers are a serial port on USB (`/dev/ttyACM*`), found by
themselves too; they need the group `dialout` (Fedora, Debian, Ubuntu) or
`uucp` (Arch Linux) instead of `lp`. Bluetooth is not supported yet.

Each printer family is a backend; adding one does not touch the rest of
the app, see [CONTRIBUTING.md](../CONTRIBUTING.md#adding-a-printer).
Developed with a VC-500W, an EOS2/300 and, for the system printer,
CUPS's simulated IPP Everywhere printer and an HP LaserJet Pro 4002. Not supported yet: continuous
media and the cutter of cab printers.
