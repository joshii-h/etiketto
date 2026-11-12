# Design and print

## Content

Elements are placed one after another along the label. Order them with
[[ui:Move up]] and [[ui:Move down]].

![Editing a text element](media/expand.png#only-light)
![Editing a text element](media/expand-dark.png#only-dark)

- **Text**: any installed font, colour, several lines. [[ui:Size]] 0 fills
  the label height, otherwise fixed in pt.
- **Image**: photos and graphics; drag and drop works too.
- **Code**: QR code, Code 128 or EAN-13.
- **Symbol**: a pictogram from the symbol library, see below.

## Symbols

[[ui:Symbol…]] opens the symbol library: over 7000 pictograms such as
arrows, warnings, recycling, tools, food or weather, from Material Design
Icons. Search by name, for example "arrow" or "recycle". The names and
categories come from the icon set and are English, so the search is in
English too. The list next to the search shows one category;
[[ui:Recently used]] shows the symbols chosen last. Brand logos of the icon
set are left out.

- [[ui:Size]] 0 fills the label height, otherwise the symbol has this
  height.
- [[ui:Rotation]]: a quarter turn at a time, clockwise.
- The colour is printed on colour printers. Black-and-white printers print
  symbols in black, with sharp edges.

The label file keeps the outline of each symbol, so a label looks the same
in later versions. Older versions of Etiketto cannot open labels with
symbols and say that the label needs a newer version.

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
