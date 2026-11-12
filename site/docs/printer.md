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
  Run `sudo usermod -aG lp $USER`, then log out and in again. NIIMBOT
  printers use another group, see [NIIMBOT](#niimbot).
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

## Brother QL { #brother-ql }

Etiketto prints on Brother QL label printers from the QL-500 to the
QL-1115NWB, red and black too on the QL-800, QL-810W and QL-820NWB. This
has not been tried on a printer yet: it follows the protocol as others
documented it. Reports of what works and what does not are very welcome.

- [[ui:Printer model]]: the QL appears twice. Choose
  "Brother QL, continuous rolls" for continuous DK rolls (the label is as
  long as its content or has a fixed length) and
  "Brother QL, die-cut labels" for die-cut DK labels of a fixed size.
  Both use the same printer and settings. If the printer has the other
  kind of roll loaded, Etiketto offers to [[ui:Switch]].
- Network: models with Wi-Fi or LAN are reached on port 9100.
  [[ui:Search for printers]] finds them, or enter the address.
- USB: plug the printer in; [[ui:Search for printers]] finds it. On the
  QL-700 and the QL-800 series, switch off "Editor Lite" first (hold its
  button until the green light goes out), otherwise the printer appears
  as a USB drive.
- Before each print Etiketto reads which roll is loaded. A continuous
  roll of another width is printed scaled to it after a warning
  ([[ui:Wrong roll]]); die-cut labels of another size get a warning too
  ([[ui:Different labels loaded]]).
- Die-cut labels: choose the [[ui:Label size]] of the roll. The printer
  finds the labels by itself; leave the [[ui:Gap]] at 3 mm. About 1.5 mm
  at the sides and 3 mm at both ends of each label cannot be printed: a
  [[ui:Safety margin]] of 3 mm keeps content away from there.
- Black and white without dithering: text and codes stay sharp, photos
  become hard black and white, as the preview shows.

### Print options { #brother-ql-print }

- [[ui:Cut]]: [[ui:Full cut]] after every label,
  [[ui:Cut after the last label]] keeps the copies together as one strip,
  [[ui:No cut]]. The QL-500 has no cutter and no cut option.
- [[ui:Colours]]: [[ui:Red and black]] appears on the QL-800 series when a
  62 mm continuous roll is loaded; red parts of the label print red on the
  red and black roll DK-22251. That roll needs a red and black job even
  for black labels: if the choice does not fit the loaded roll, the
  printer refuses the job, Etiketto prints it the other way and says so.
  From then on the choice follows the roll.
- [[ui:Quality]]: [[ui:High resolution (600 dpi)]] doubles the resolution
  along the roll; printing takes longer. Offered on the QL-570, QL-580N,
  QL-700, QL-710W, QL-720NW, the QL-800 series and the QL-1100 series.

### Problems { #brother-ql-problems }

- [[ui:Reachable · the printer reports no status]]: some models
  do not answer status requests over the network. Printing works, but
  Etiketto cannot check the roll first.
- "The loaded roll does not match the label": a die-cut roll is loaded
  for a label on continuous roll, or the other way round. Load the other
  roll or choose the other entry under [[ui:Printer model]].
- Out of labels: load a new roll. Cover open: close it. Label jammed:
  open the cover and remove it, check the cutter.
- Red parts print black: red and black only works on the QL-800 series
  with the DK-22251 roll.

## cab label printers { #cab }

Etiketto prints on cab printers with a 4 inch print head: EOS2, EOS5,
SQUIX 4 and MACH 4S, at 203, 300 or 600 dpi. Developed with an EOS2/300.

- Network: the printer needs an address in your network (DHCP or set on its
  display). Etiketto uses the "RawIP" port 9100 with the JScript interpreter,
  both are the printer's defaults (Setup > Interfaces > Network services and
  Setup > Interpreter).
- USB: plug the printer in; [[ui:Search for printers]] finds it. Printing
  over USB has not been tried on a printer yet.
- Labels: die-cut labels with a gap, label sensor "Gap Sensor". Etiketto
  sets the sensor for each job. Continuous media and a cutter are not
  supported yet.
- [[ui:Print method]]: [[ui:As set on the printer]] keeps the printer's
  own setting. [[ui:Thermal transfer (ribbon)]] for labels printed with a
  ribbon, [[ui:Thermal direct]] for labels that print without one. The
  printer then prints at the speed set on it, if its web interface tells
  it, else at 50 mm/s. [[ui:Printer details]] show the method set on the
  printer.
- Heat and tear-off position stay as set on the printer.

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
  direct mode. Load or remove the ribbon, or choose the other
  [[ui:Print method]].
- Labels with a ribbon print blank or smeared, the ribbon does not move:
  the printer is set to thermal direct. Choose
  [[ui:Thermal transfer (ribbon)]] under [[ui:Print method]].
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
- [[ui:Tape feed]]: blank tape before and after each label, at least 2 mm
  (PT-P900 series: 1 mm).
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

## NIIMBOT { #niimbot }

Etiketto prints on NIIMBOT label printers over USB: B1, B21, B3S, B4, D11,
D110, D101 and related models. It has not been tried on a printer yet;
please report how it works.

- USB: plug the printer in and switch it on; [[ui:Search for printers]]
  finds it. Otherwise enter its port, for example `/dev/ttyACM0`, under
  [[ui:Host name or IP address]].
- The port belongs to the group `dialout` (Fedora, Debian, Ubuntu) or
  `uucp` (Arch Linux), not `lp`. Run `sudo usermod -aG dialout $USER`
  (on Arch Linux: `uucp`), then log out and in again.
- Bluetooth is not supported yet. Older B3S with a micro-USB port do not
  answer over USB; the B3S P with USB-C does.
- [[ui:Label size]]: the labels on the roll, the width across the printer.
  On the D11, D110 and D101 the roll is narrow (12 mm on a D11): choose
  [[ui:Along the roll]] under [[ui:Content direction]] for text along the
  label.
- A label narrower than the print head is printed in its middle. One a
  little wider is cut to the middle: a B1 prints 48 mm of a 50 mm label.
  Keep 1 mm or more as [[ui:Safety margin]].
- In the printer group: [[ui:Print density]] (higher prints darker) and
  [[ui:Label sensor]] (labels with gaps, black marks, a continuous roll or
  transparent labels, as loaded). Etiketto offers what the connected model
  takes and remembers the choice.

### Problems { #niimbot-problems }

- No permission for the serial port: add your user to the group, see above.
- [[ui:Printer not reachable]] right after plugging in: some systems probe
  new serial ports for a modem first. Wait a few seconds, then
  [[ui:Refresh]].
- Cover open, out of labels or a low battery: close the cover, load a roll
  or charge the printer, then [[ui:Refresh]].
- Printing fails after some copies: the message says how many labels were
  already printed. The job is canceled, nothing prints later on its own.
- The printer is found, but printing reports an unknown model: Etiketto
  does not know its model number yet. Please
  [[ui:Report a problem…]], the report contains the number.

## System printer (CUPS, IPP) { #system }

Prints on the printers your computer knows: printers set up in the system
settings (also over USB) and printers in the network that support IPP
Everywhere or AirPrint, as most office printers do. The label is printed
as one page of exactly its size, unscaled.

- [[ui:Search for printers]] lists both. Otherwise enter the printer's
  address or the name of its CUPS queue under
  [[ui:Host name or IP address]].
- [[ui:Label size]]: the paper size, or [[ui:Custom size]].
- [[ui:Print on]] [[ui:Label sheet]]: several labels on a sheet of
  self-adhesive labels, see [Label sheets](sheets.md).
- Most office printers cannot print the outer 4 to 5 mm of a page: set the
  [[ui:Safety margin]] to 5 mm. On label sheets Etiketto warns when a
  label reaches into what the printer reports.

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

## Label printers with ZPL, EPL2, TSPL, ESC/POS or DYMO { #raw }

Etiketto prints on label printers that take the label as an image in their
own printer language. These printers have not been tried with Etiketto yet:
if you have one, a report of what works and what does not helps a lot
([[ui:Report a problem…]]).

| Printer model in Etiketto | Printers |
|---|---|
| Zebra ZPL | Zebra GK, GX, ZD, ZT and other printers with ZPL, also ZPL compatible printers of other makers |
| Zebra EPL2 | Zebra LP/TLP 2824 and 2844, GK420 and GX420 set to EPL2 |
| TSPL (TSC, Munbyn, iDPRT, Xprinter, Rollo) | TSC printers and most 4 × 6 in shipping label printers |
| [[ui:Receipt printer (ESC/POS)]] | Epson TM and compatible receipt printers on 58 or 80 mm paper |
| DYMO LabelWriter | LabelWriter 300, 400 and 450 series, 4XL (not the 550 and 5XL) |

- Connection: over the network on port 9100, or USB. Plug a USB printer in
  and use [[ui:Search for printers]]. Some low-cost shipping label printers
  do not show up as a printer on USB; Etiketto cannot find those.
- [[ui:Resolution]]: choose the resolution of the print head (203, 300 or
  600 dpi). Zebra printers report it: [[ui:As reported by the printer]].
- [[ui:Label sensor]]: [[ui:As set on the printer]] keeps the printer's own
  setting. [[ui:Gap between labels]] for die-cut labels,
  [[ui:Black mark]] for labels with a black mark on the back,
  [[ui:Continuous media]] for a roll without gaps. Set the [[ui:Label size]]
  and the [[ui:Gap]] (or the height of the mark) like the roll.
- [[ui:After printing]]: [[ui:Tear off]], [[ui:Peel off]] or
  [[ui:Cut off]], if the printer has a peeler or a cutter.
  [[ui:As set on the printer]] leaves the printer's own setting.
- [[ui:Darkness]] and [[ui:Speed]] change the printer's setting for this
  print; [[ui:As set on the printer]] leaves it alone.
- [[ui:Print upside down]]: for a printer that puts out the label the
  wrong way round.
- Receipt printers print the label on endless paper: choose the paper width
  under [[ui:Tape width]]. The printable width is narrower than the paper
  (48 mm on 58 mm paper, 72 mm on 80 mm paper). [[ui:Cut]] in the print
  dialog cuts the paper after each label.

### Problems { #raw-problems }

- [[ui:Reachable · the printer reports no status]]: the printer takes jobs
  but answers no questions, which many of these printers do (EPL2 printers
  never answer). Problems such as an open cover then show on the printer
  only.
- The printer did not report its resolution in time: choose it under
  [[ui:Resolution]] instead of [[ui:As reported by the printer]].
- The label comes out upside down: switch on [[ui:Print upside down]], and
  please report it with the printer model; the orientation could not be
  tried on a printer yet.
- Print too pale or too dark: change [[ui:Darkness]].
- The printer prints several blank labels or stops between labels: the
  [[ui:Label sensor]] does not match the roll, or the printer has to measure
  the labels again (most printers do that with a long press on Feed).
- Nothing is printed and the printer shows no problem: check that its
  printer language is the one chosen in Etiketto (many printers can switch
  between ZPL and EPL2, or between TSPL and ESC/POS).

## Help

Something else? [[ui:Report a problem…]] in the main menu (also offered with
error messages) opens a prefilled issue in the browser with version, system
and printer. You see everything before sending it.
