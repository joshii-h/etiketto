# Design and print

## Content

Elements are placed one after another along the label. Order them with
[[ui:Move up]] and [[ui:Move down]].

![Editing a text element](media/expand.png#only-light)
![Editing a text element](media/expand-dark.png#only-dark)

- **Text**: any installed font, colour, several lines. [[ui:Size]] 0 fills
  the label height, otherwise fixed in pt.
- **Image**: photos and graphics; drag and drop works too.
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
- Labels made with earlier versions: QR codes come out up to about a
  third smaller (their blank margin is now inside the element), barcodes
  slightly wider.

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

## Print

![Print dialog](media/print.png#only-light)
![Print dialog](media/print-dark.png#only-dark)

- [[ui:Copies]]: number of labels.

The print dialog only shows what the printer can do; the options of each
printer are listed under [Printers](printer.md). Printers without such
options print with the settings made on the printer itself.

[[ui:Export PNG…]] saves the label as image.
