# Design and print

## Content

Elements are placed one after another along the label. Order them with
[[ui:Move up]] and [[ui:Move down]].

![Editing a text element](media/expand.png#only-light)
![Editing a text element](media/expand-dark.png#only-dark)

- **Text**: any installed font, colour, several lines. [[ui:Size]] 0 fills
  the label height, otherwise fixed in pt.
- **Image**: photos and graphics; drag and drop works too.
- **Symbol**: a pictogram from the symbol library, see below.
- **Code**: [[ui:Type]] offers 2D codes (QR code, Data Matrix, PDF417,
  Aztec), linear barcodes (Code 128, Code 39, Code 93, EAN-13, EAN-8, UPC-A,
  UPC-E, ITF-14) and GS1 codes (GS1-128, GS1 DataMatrix). Below the type it
  says what the type is for and what it accepts.
- EAN, UPC and ITF-14: enter the digits without the check digit, it is
  added; with it, it is checked.
- GS1-128 and GS1 DataMatrix: application identifiers in brackets, each
  followed by its value, e.g. `(01)09501101530003(17)261231(10)AB12`.
  Etiketto checks identifiers, lengths, dates and check digits and says
  which identifier is wrong. GS1-128 shows the identifiers in brackets
  below the bars.
- A GS1 value with round brackets in it: write the identifiers in square
  brackets, e.g. `[01]09501101530003[21]A(B)C`.
- ITF-14 and GS1-128 on shipping cartons: GS1 asks for modules of at least
  0.495 mm. On narrow labels they come out narrower: fine for your own
  scanners, too small for carton labels read in logistics.
- Code 39: a `$`, `/`, `+` or `%` before a capital letter is read as
  another character by scanners set to Full ASCII; Etiketto warns.
- Content a code type cannot take is not changed silently: the element is
  crossed out and a warning says why.
- Labels with a code type other than QR code, Code 128 and EAN-13 do not
  open in earlier versions of Etiketto: they say the label needs a newer
  version.
- Codes print sharp on every printer: each bar and square is a whole
  number of printer dots, as close to the designed width as the element's
  space allows, with the blank margin a scanner needs. The code may come
  out slightly smaller than designed, centred in its space.
- A code whose bars are only one printer dot wide, or without room for its
  blank margin, may not scan: Etiketto warns before printing, make the
  code larger. A code that does not fit at all is not printed.
- QR codes with characters beyond plain ASCII (umlauts, °, £ …) are now
  marked as UTF-8, so scanners read them correctly. Labels made with
  earlier versions print such a QR code with a slightly different pattern.
- Labels made with earlier versions: QR codes come out up to about a
  third smaller (their blank margin is now inside the element), barcodes
  slightly wider.

## Symbols

[[ui:Symbol…]] opens the symbol library: over 7000 pictograms such as
arrows, warnings, recycling, tools, food or weather, from Material Design
Icons. Search by name, for example "arrow" or "recycle". The names and
categories come from the icon set and are English, so the search is in
English too. The list next to the search shows one category;
[[ui:Recently used]] shows the symbols chosen last. The "Brand / Logo"
category and further icons showing brands or trademarks are left out.

- [[ui:Size]] 0 fills the label height, otherwise the symbol has this
  height.
- [[ui:Rotation]]: a quarter turn at a time, clockwise.
- The colour is printed on colour and office printers. Black-and-white label
  printers print symbols in black, or in white if the colour is light, with
  sharp edges.

The label file keeps the outline of each symbol, so a label looks the same
in later versions. Older versions of Etiketto cannot open labels with
symbols and say that the label needs a newer version.

Texts and codes can contain [variables](#variables), e.g. a date or a
running number.

## Label

Endless tape:

- [[ui:Automatic length]]: the label is as long as its content. Off: fixed
  [[ui:Length]], content aligned and scaled down if needed.

For every label:

- [[ui:Spacing]]: space at the ends and between elements.
- [[ui:Edge spacing]]: extra space along the label edges.
- [[ui:Background colour]], [[ui:Background image]]: printed edge to edge.
- [[ui:Border]]: width, 0 = none; always inside the safety margin.
- [[ui:Units]]: millimetres or inches, by default from the system language.

Die-cut labels have a fixed size instead of a length:

![Die-cut label](media/die-cut.png#only-light)
![Die-cut label](media/die-cut-dark.png#only-dark)

- [[ui:Label size]]: a common size, or [[ui:Custom size]] with [[ui:Width]]
  (across the roll) and [[ui:Height]] (along the roll), as on the label box.
- [[ui:Gap]]: space between two labels on the roll, usually 2 to 3 mm.
  Before printing Etiketto compares height and gap with the distance the
  printer measured.
- [[ui:Content direction]]: [[ui:Across the roll]] for labels read with
  the roll running from top to bottom, [[ui:Along the roll]] like on tape.
- [[ui:Alignment]]: where the content sits if it is shorter than the label.

[[ui:Safety margin]] (printer setting, default 1.5 mm): content and border
keep this distance from the label edges. Printers lose a little at the
edges, and long labels can run slightly askew. If text is cut off at the
edge, increase it.

## Shipping labels

Carriers and marketplaces send shipping labels as a PDF or an image,
often an A4 page with the label in one half or quarter and a receipt or
instructions next to it. [[ui:Shipping label…]] in the main menu (or a PDF
dropped onto the window) opens such a file and makes a new label of it.

- Etiketto looks for the label on the page: a large area with the shape
  of a label, best with a printed frame and a barcode; a page with one
  label filling it is taken whole. Check the selection:
  drag it, drag its edges, or draw a new one. [[ui:Next suggestion]] shows
  other areas that could be the label, [[ui:Whole page]] selects all of it.
- [[ui:Page]]: the page of a PDF with several pages.
- [[ui:Label size]]: 4 × 6 in (101.6 × 152.4 mm), 100 × 150 mm,
  100 × 200 mm, a 62 mm roll that is as long as the label needs, or A6 and
  A4 paper on the system printer. Only sizes the printer can print are
  offered; on endless tape the label runs along the tape.
- A label printed sideways on the page is turned upright for the size;
  [[ui:Rotate left]] and [[ui:Rotate right]] turn it by hand.
- [[ui:Scale]]: [[ui:Whole area, centred]] keeps all of the area,
  [[ui:Fill the label, edges cut off]] fills the label completely.
- [[ui:Create label]] replaces the open label. The area stays an element
  of the label: [[ui:Area]] in its row opens the selection again.

A PDF page is drawn right at the resolution it is printed at, without
smoothing: bars and text come out as whole dots, so barcodes stay
readable. Lines thinner than a dot are printed at least one dot wide
(sometimes two), and in black even if they are light grey or coloured;
white lines thinner than a dot on black may vanish. Label printers get the
label at their own resolution. The system printer gets it without loss:
as PWG Raster drawn at the printer's resolution, or as a PDF drawn at
300 dpi, which a printer of another resolution scales itself (on a 203 dpi
printer the edges of bars may then soften: check that the barcode scans).
Only printers that take nothing but JPEG get a compressed copy.

Images are enlarged by repeating pixels, evenly only by whole multiples
(otherwise some pixels come out one dot wider than others); reduced, the
pixels that fall together are averaged into grey.

On black-and-white printers the area is looked at piece by piece. Where
it is black and white but for grey edges (barcodes and text, also from a
scan with grey ink on grey paper), it is cut at the middle between its
own black and white, so bars keep clean edges; images are enlarged
smoothly for this, so the edges fall between the pixels of the scan.
Where it has grey of its own (a grey logo, grey text down to light grey,
a shaded band), the grey is printed as a dot pattern; black text on it
gets a narrow edge of paper so that it stays readable. A scan or photo of
a label should have at least 150 dpi; at 100 dpi only a clean one keeps
all its bars.

The label file keeps the whole page the area was taken from, also outside
the area (addresses, tracking numbers). Of a PDF with several pages only
the chosen page is kept if it can be copied unchanged, otherwise the whole
document (the dialog then says so). Unchanged is strict: a copy that differs
by more than rounding anywhere, even by a few dots, is not used; pages with
embedded fonts that are drawn slightly differently when copied (TeX, for
example) keep the whole document. Of a TIFF with several pages only the
chosen page is kept.

Opening PDF files needs Poppler (poppler-glib); the Flatpak brings it
along, otherwise install it from your distribution (see the README).
Older versions of Etiketto refuse to open labels with a shipping label and
say that the label needs a newer version.

Most shipping label rolls are direct thermal labels: they need no ribbon,
but the print fades in heat and sunlight within months. That is fine for
a parcel on its way; for labels that must last, use thermal transfer
labels with a ribbon.

## Print

![Print dialog](media/print.png#only-light)
![Print dialog](media/print-dark.png#only-dark)

- [[ui:Copies]]: number of labels.

The print dialog only shows what the printer can do; the options of each
printer are listed under [Printers](printer.md). Printers without such
options print with the settings made on the printer itself.

[[ui:Export PNG…]] saves the label as image.

## Variables { #variables }

Variables are filled in when printing, in texts and in the content of
codes. [[ui:Insert variable]] next to the text or the content offers the
common ones; type the others.

| Variable | Result |
|---|---|
| `{=date}` | today, as the system language writes dates |
| `{=date:%d.%m.%Y}` | today in a format of your own, e.g. 31.12.2026 |
| `{=date+30d}` | in 30 days, e.g. "best before"; also `w` (weeks), `m` (months), `y` (years), `h` (hours), combined like `{=date+1m-1d}` |
| `{=time}`, `{=time:%H:%M}` | the time |
| `{=counter}` | a running number |
| `{=counter:0001}` | with leading zeros, as many digits as the pattern |
| `{=copy}`, `{=copies}` | number of this copy and of all copies |
| `{=id}`, `{=id:12}` | a short random ID, 8 characters by default |
| `{=uuid}` | a random UUID |

Formats use the letters of
[strftime](https://docs.python.org/3/library/datetime.html#format-codes):
`%d` day, `%m` month, `%Y` year, `%H:%M` hours and minutes, `%B` the name
of the month.

The counter goes up with every printed label, also from copy to copy:
5 copies get 5 numbers. Set the [[ui:Next number]] and the [[ui:Step]] in
the [[ui:Counter]] group below the content; it appears once a text uses
the counter. After printing the label remembers the next number: save it
to keep it.

On one label every variable has one value: a counter or an ID used in the
text and in the QR code is the same number. The preview shows the values
the next print uses.

To print a brace, double it: `{{=date}}` prints `{=date}`. Texts without
variables print exactly as typed, unless the label has a data file for
[serial labels](serial.md): then `{{` and `}}` always print one brace.

Dates and times are in the computer's time zone. `+1d` keeps the time of
day, also when summer time starts or ends; `+2h` counts real hours. `%z`
and `%Z` give the offset and the name of the time zone.

A label with variables or a counter needs this version of Etiketto or a
newer one: older versions refuse to open it instead of printing `{=date}`
as it is.

## Command line { #command-line }

`etiketto print` prints a saved label without opening a window, also on a
computer without a screen:

```sh
etiketto print label.etiketto
etiketto print label.etiketto --copies 5 --counter-start 100
etiketto print label.etiketto --set title="Batch 7" --set lot=A12
etiketto print label.etiketto --printer system --host ipp://printer.local/ipp/print
etiketto print label.etiketto --copies 3 --dry-run --output preview.png
etiketto print shelf.etiketto --data items.csv --rows 3-10
etiketto list-printers
etiketto --version
```

- Without `--host` Etiketto uses the printer set in the app, or the only
  one it finds on the network. `etiketto list-printers` shows model,
  address and name of each printer found.
- `--set NAME=VALUE` replaces the text of the element with that
  [[ui:Name for the command line]] (in the element's settings), or fills in
  a variable of your own: `{=lot}` in the label, `--set lot=A12` when
  printing; a name may use letters of any language. Variables in a new
  text are filled in; `--set-literal NAME=VALUE` takes the value as it
  is. `--set date=2026-12-31` prints with another date.
- With a counter the next number is saved in the label file after
  printing, also when the printer fails half-way or the print is stopped
  with Ctrl+C. If the file cannot be written, nothing is printed;
  `--no-save-counter` prints anyway and saves nothing, so the next print
  repeats the numbers. The label file is locked while printing: a second
  `etiketto print` of the same file waits. The window reads the saved
  number again before printing and saving.
- It prints with the choices last made in the app for that printer
  model: cut, colours, quality, chain printing, high resolution, tape feed,
  the printer settings and [[ui:Print outlines]] on a label sheet.
  `--printer-defaults` takes the model's defaults for these instead (no
  outlines); the printer's address and the [[ui:Safety margin]] stay as
  set in the app.
- Some printers are asked before the label is drawn, because it depends
  on what they report (the tape, the resolution); a printer that does not
  answer within 5 s (2 s for `--dry-run`) gets the label drawn without.
  A question still on its way then gets up to 10 s more to end before
  the label is sent (Etiketto says so); an answer in that time is used.
  `--no-query` skips the question.
- Serial labels: one label for each row of the data file, `--copies` of
  each; `--data` gives the data file (by default the label's own, if it
  lies in the label's folder), `--sheet` the sheet, `--rows` the rows
  (`3-10`, `5`, `7-` to the last). The counter goes on from row to row.
  A placeholder without a column or a code a row cannot make stops the
  print before the first label. See [Serial labels](serial.md).
- `--dry-run` only checks and renders: it lists the texts of each label;
  `--output file.png` saves each label as [[ui:Export PNG…]] does: as
  printed, on the whole tape, at most 600 dpi like the preview (a large
  page coarser), with its resolution (`file-1.png`, `file-2.png` … if
  they differ; `-` writes the first to standard output). The first label
  is also drawn as the printer would get it. With `--output` every label
  is drawn, and a code that does not fit stops the dry run (exit code 4);
  without it the labels after the first are not drawn: their columns and
  codes are checked, not whether a code fits.

Exit codes: 0 printed, 1 printer problem or no printer found, 2 wrong
arguments, 3 label file cannot be read, still locked by another print or
output not written, 4 the label cannot be printed (empty, unknown or
unclosed variable, date before the year 1 or after 9999, unknown column,
a code a row cannot make), 5 the counter
cannot be saved (nothing printed; or printed, but saving failed),
130 interrupted.

In the Flatpak the label file has to be handed over with
`--file-forwarding`, a data file too (each between `@@`; the label's own
data file is only reached when handed over); write images to standard
output:

```sh
flatpak run --file-forwarding xyz.joshuahirsig.Etiketto print @@ label.etiketto @@
flatpak run --file-forwarding xyz.joshuahirsig.Etiketto print @@ label.etiketto @@ --data @@ rows.csv @@
flatpak run --file-forwarding xyz.joshuahirsig.Etiketto print --dry-run --output - @@ label.etiketto @@ > preview.png
```

A file handed over this way is reached through the document portal. The
next counter value is written there like any save, but the portal may not
support locking the file: two prints of the same file at the same moment
are then not kept apart. If the portal refuses to write, nothing is
printed (exit code 5).

Writing the counter back through the document portal has not been tried
yet. After the first print from the Flatpak, open the label and check
that [[ui:Next number]] went on; if not, give the start with
`--counter-start` on every print.
