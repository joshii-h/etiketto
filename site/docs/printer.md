# Printers and troubleshooting

Choose the printer under [[ui:Printer model]]; each one has its own
section below.

## All printers { #common }

- [[ui:No printer found]]: printer on and in the same network? The search
  needs the Avahi service (installed on most desktops, any init system);
  systemd-resolved alone resolves `.local` names but cannot search. Otherwise
  enter the address under [[ui:Host name or IP address]].
- [[ui:Printer not reachable]]: check the address, the printer may have a new
  IP. Switch the printer off and on again.
- [[ui:Printer needs attention]]: the printer is reachable but reports a
  problem, the text below says what to do. Clear it on the printer, then
  [[ui:Refresh]].
- Content cut off at the edge or askew: increase the [[ui:Safety margin]].
- [[ui:content scaled down to fit]]: the content does not fit the label.
  Make the label longer, use [[ui:Automatic length]] on tape, or shorten
  the content.

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

## Help

Something else? [[ui:Report a problem…]] in the main menu (also offered with
error messages) opens a prefilled issue in the browser with version, system
and printer. You see everything before sending it.
