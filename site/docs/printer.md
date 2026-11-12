# Printers and troubleshooting

Choose the printer under [[ui:Printer model]]; each one has its own
section below.

## All printers { #common }

- [[ui:No printer found]]: printer on and in the same network? The search
  needs the Avahi service (installed on most desktops, any init system);
  systemd-resolved alone resolves `.local` names but cannot search. Otherwise
  enter the address under [[ui:Host name or IP address]].
- [[ui:Printer not reachable]]: check the address, the printer may have a new
  IP. Switch the printer off and on again. On USB: check the cable and
  search again.
- No permission for the USB port: your user needs to be in the group `lp`.
  Run `sudo usermod -aG lp $USER`, then log out and in again.
- USB printer not found: check the cable, switch the printer on and wake it
  up (a sleeping printer can drop off USB), then search again.
- "The printer is busy" on USB: only one program can use a USB printer at a
  time. Close other label software, or wait until the running print is done.
- [[ui:Printer needs attention]]: the printer is reachable but reports a
  problem, the text below says what to do. Clear it on the printer, then
  [[ui:Refresh]].
- Content cut off at the edge or askew: increase the [[ui:Safety margin]].
- [[ui:content scaled down to fit]]: the content does not fit the label.
  Make the label longer or larger, use [[ui:Automatic length]] on tape, or
  shorten the content.

## Brother VC-500W { #vc500w }

### Wi-Fi setup { #wifi }

Etiketto does not set up Wi-Fi. Use a phone or the printer's web interface
once:

1. Hold the Wi-Fi button until it turns from blue to white.
2. Connect phone or computer to the open network "Brother VC-500W…".
3. Open `https://192.168.0.1` (accept the certificate warning) or Brother's
   Color Label Editor app.
4. Choose your network and enter the password. Only 2.4 GHz, WPA or open.

Later, the web interface is at `https://` and the printer's address:
"Network Wi-Fi Setup" changes the network (or "click here" to enter it by
hand), "AirPrint Options" sets name, location and firmware updates.

### USB { #vc500w-usb }

Plug the printer in; [[ui:Search for printers]] finds it. Printing over USB
has not been tried on a printer yet and follows the observations of others:
the printer may not answer over USB at all (then the query ends with
[[ui:Printer not reachable]]), and it may not cut by itself, then swipe
across its panel. Wi-Fi is the tried way.

### Cassettes

| Cassette | Width |
|---|---|
| CZ-1001 | 9 mm (3/8″) |
| CZ-1002 | 12 mm (1/2″) |
| CZ-1003 | 19 mm (3/4″) |
| CZ-1004 | 25 mm (1″) |
| CZ-1005 | 50 mm (2″) |

Etiketto shows the inserted cassette, remaining tape, printable width, lot
number and the presumed production date. ZINK paper ages: old paper prints
paler and yellowish, Etiketto warns about old cassettes. Store cassettes cool
and dry.

Cleaning: insert the cleaning cassette CK-1000, cleaning starts by itself.
Do it when stripes appear, or every 20 to 30 labels.

### Print options { #vc500w-print }

- [[ui:Cut]]: [[ui:Full cut]] separates the labels. [[ui:Half cut]] cuts
  through the label, the backing holds them together. [[ui:No cut]]: the
  printer waits, swipe its panel to cut.
- [[ui:Colours]]: [[ui:Vivid]] for the strongest colours, [[ui:Normal]]
  (Brother: Standard) for softer ones, black and white (400 lpi) for text and
  codes.
- Colour correction: brightness and colour balance (red, green, blue), for
  this print only, not stored in the printer.

### Problems { #vc500w-problems }

- [[ui:Wrong cassette]]: the label was designed for another width. Change
  [[ui:Tape width]] or the cassette.
- [[ui:No cassette inserted]] or cassette not recognised: reinsert the
  cassette until it clicks.
- No paper: the cassette is empty, replace it.
- Paper jam: open the cover, remove the jammed label, reinsert the cassette.
- Cover open: close the cover.
- Warming up, cooling down, cleaning: wait, printing continues by itself.
- Swipe the panel to cut: the label was printed without cut, swipe across the
  panel.
- Pale colours, yellow tint: old paper (see the cassette's age), or
  [[ui:Normal]] instead of [[ui:Vivid]]. With old cassettes try
  [[ui:Brightness]] −1 to −3 under [[ui:Colour adjustment]].
- Background barely visible: ZINK hardly prints very light pastel shades,
  choose a medium shade.
- Stripes: clean with the CK-1000.
- Content askew: insert the cassette straight.

## cab label printers { #cab }

Etiketto prints on cab printers with a 4 inch print head: EOS2, EOS5,
SQUIX 4 and MACH 4S, at 203, 300 or 600 dpi. Developed with an EOS2/300.

- Network: the printer needs an address in your network (DHCP or set on its
  display). Etiketto uses the "RawIP" port 9100 with the JScript interpreter,
  both are the printer's defaults (Setup > Interfaces > Network services and
  Setup > Interpreter).
- USB: plug the printer in; [[ui:Search for printers]] finds it.
- Labels: die-cut labels with a gap, label sensor "Gap Sensor". Etiketto
  sets the sensor for each job. Continuous media and a cutter are not
  supported yet.
- Print method (thermal direct or with ribbon), heat, speed and tear-off
  position stay as set on the printer.

### Problems { #cab-problems }

- [[ui:Different labels loaded]]: the printer measured another distance
  between the labels than height plus gap of your label. Check
  [[ui:Label size]] and [[ui:Gap]], or load the right roll. Press Feed on
  the printer once after changing the roll so it measures again.
- A print that stopped because of a problem is cancelled, so printing again
  does not give extra labels.
- Out of labels: load a new roll, the printer then synchronises by itself.
- Print head open: close the print head.
- Next label not found: labels not threaded through the label sensor, or
  labels without gaps (continuous media is not supported yet).
- Ribbon problem: no ribbon in thermal transfer mode, or a ribbon in thermal
  direct mode.
- Paused: resume the printer on its display.
- Print too pale or too dark: change the heat level in the printer's setup.

## Brother P-touch { #ptouch }

Etiketto prints on Brother P-touch tape printers with TZe tape and HSe
heat-shrink tube: the PT-P700, PT-P750W, PT-E550W, PT-P710BT, the PT-P900
series, the PT-D series and older models such as the PT-2430PC. Not tried
on a printer yet.

- USB: plug the printer in; [[ui:Search for printers]] finds it.
- Network (PT-P750W, PT-E550W, PT-P900W, PT-P950NW): the search finds the
  printer, or enter its address. Etiketto uses port 9100.
- Bluetooth is not supported; models with Bluetooth print over USB.
- Editor Lite: with the Editor Lite lamp on, the printer is a USB drive and
  cannot print. Switch Editor Lite off, then search again.

### Tape { #ptouch-tape }

- Etiketto shows the inserted tape, its type (laminated, heat-shrink tube,
  ...) and the printable width under [[ui:Printer details]].
- The label fills the printable part of the tape. The edges of the tape
  stay blank: on 12 mm tape about 9.9 mm are printed.
- Heat-shrink tube: choose the nearest [[ui:Tape width]] (12 mm for an
  11.2 mm tube).
- Print in black: on coloured tape the tape's ink colour prints. Images
  are printed in black and white without dithering.

### Print options { #ptouch-print }

- [[ui:Cut]]: [[ui:Full cut]] cuts each label off. [[ui:Half cut]] cuts
  through the label, the backing holds the labels together. [[ui:No cut]]
  prints the labels as one strip. The strip is cut after the last label.
  Models with a cutter lever have no cut setting.
- [[ui:Chain printing]]: the last label stays in the printer and comes out
  with the next print. Saves about 25 mm of tape per print.
- [[ui:High resolution]]: finer print along the tape, slower.
- [[ui:Tape feed]]: blank tape before and after each label, at least 2 mm.
- The first label after switching on starts with a blank piece: the cutter
  sits behind the print head.

### Problems { #ptouch-problems }

- [[ui:Wrong cassette]]: another tape width is inserted. The label is
  scaled to the tape.
- Cover open, no tape, tape not recognised: close the cover, insert the
  tape until it clicks.
- Batteries low: connect the power adapter.
- Print head too hot: wait a few minutes.
- Model not supported: Etiketto knows the models listed above.

## System printer (CUPS, IPP) { #system }

Prints on the printers your computer knows: printers set up in the system
settings (also over USB) and printers in the network that support IPP
Everywhere, as most office printers do. A network printer that only offers
AirPrint can be printed on once it is set up in the system settings. The
label is printed as one page of exactly its size, unscaled.

- [[ui:Search for printers]] lists both. Otherwise enter the printer's
  address or the name of its CUPS queue under
  [[ui:Host name or IP address]].
- [[ui:Label size]]: the paper size, or [[ui:Custom size]].
- Most office printers cannot print the outer 4 to 5 mm of a page: set the
  [[ui:Safety margin]] to 5 mm.

### Problems { #system-problems }

- Out of paper, paper jam or cover open while printing: Etiketto shows it
  and waits. Fix it at the printer within 5 minutes and printing goes on.
- Out of toner or ink, or a problem before printing: fix it as the
  printer's display shows, then [[ui:Refresh]].
- Printing fails after some copies: the message says how many labels were
  already printed. The rest is canceled, nothing prints later on its own.
- The label is printed smaller or moved: the printer or its driver scales
  the page. Turn off "fit to page" in the printer's setup.
- Other jobs are printing first: Etiketto says the label was sent, and the
  printer prints it when its turn comes.
- The job is held in the queue (until it is released, or until a password
  is entered): Etiketto cancels it. Check the queue in the system settings.

## Help

Something else? [[ui:Report a problem…]] in the main menu (also offered with
error messages) opens a prefilled issue in the browser with version, system
and printer. You see everything before sending it.
