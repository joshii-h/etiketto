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

On label printers a PDF page is drawn right at the printer's resolution,
without smoothing: bars and text come out as whole printer dots, so
barcodes stay readable. Lines thinner than a dot are printed at least one
dot wide (sometimes two), and in black even if they are light grey or
coloured; white lines thinner than a dot on black may vanish.
The system printer gets the label compressed (JPEG in a PDF) or scaled to
its resolution, which can soften the edges of bars: print one label and
check that the barcode scans.

Images are enlarged by repeating pixels, evenly only by whole multiples
(otherwise some pixels come out one dot wider than others); reduced, the
pixels that fall together are averaged into grey. A scan or photo of a
label should have at least the printer's resolution (200 to 300 dpi). On
black-and-white printers an area that is black and white but for grey
edges is printed in pure black and white, without a dot pattern; an area
with grey of its own (grey text, shaded bands) keeps it as a dot pattern.

The label file keeps the whole page the area was taken from, also outside
the area (addresses, tracking numbers). Of a PDF with several pages only
the chosen page is kept if it can be copied unchanged, otherwise the whole
document (the dialog then says so); of a TIFF with several pages only the
chosen page.

Opening PDF files needs Poppler (poppler-glib); the Flatpak brings it
along, otherwise install it from your distribution (see the README).

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
