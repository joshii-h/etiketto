# Features

Label designer for the Linux desktop. Design labels with text, images,
symbols and codes, see them exactly as they will be printed, and print them over the
network on endless tape or on die-cut labels. Tavletta talks to each
printer in its own language, finds it on the network and explains printer
problems in plain words.

GTK 4 and libadwaita, in 27 languages: English, German, French, Italian,
Spanish, Portuguese (Portugal and Brazil), Dutch, Swedish, Finnish,
Estonian, Polish, Hungarian, Russian, Ukrainian, Turkish, Arabic, Hindi,
Thai, Vietnamese, Indonesian, Malay, Korean, Japanese and Chinese
(Traditional for Taiwan and Hong Kong, Simplified).

- Finds the printer on the network by itself and shows its state
- Endless tape: as long as the content, or a fixed length
- Die-cut labels: common or custom sizes, content across or along the roll
- Label sheets on office printers: over 1700 sheets (Avery, Zweckform,
  Herma …) found by part number or size, start at any position on a partly
  used sheet, outlines for a test print; warns about the printer's margins
- Text with any installed font, a font weight (thin to black) or bold,
  italic, underline, strikethrough,
  colour, multiple lines, letter and line spacing, size fitted to the label
  or fixed in pt, shrunk to fit or wrapped at a width, vertical; white on
  black, a box or a frame with rounded corners, an outline and a shadow;
  emoji and font fallback via Pango
- Images and photos: crop (drag or numbers), turn, mirror, brightness,
  contrast, gamma, invert, rounded corners and a border; on black-and-white
  printers a threshold, Floyd-Steinberg or ordered dithering, or automatic
- Lines (with arrows), rectangles, rounded rectangles, ellipses and
  separators, solid, dashed or dotted, filled solid or with 1-bit patterns;
  every line at least one printer dot wide
- Background colour, background image, border with rounded corners, dashed,
  dotted or double, moved inwards; each element aligned at the top, middle
  or bottom and with its own spacing
- Black-and-white printers: text without anti-aliasing, codes, symbols,
  lines and borders converted on their own to sharp black and white, images
  by their own method; each element in black or white against what lies
  under it, and a warning where it would not stand out from the background
- Free layout: instead of one after another, each element at its own
  position (x and y), with its size, a code's module size and a QR code's
  error correction; switching keeps the places the elements had
- Undo and redo, keyboard shortcuts to add, duplicate, remove and move
  elements, a shortcuts window
- Codes: QR code, Data Matrix, PDF417, Aztec; Code 128, Code 39, Code 93,
  EAN-13, EAN-8, UPC-A, UPC-E, ITF-14 (with bearer bars); GS1-128 and GS1
  DataMatrix with the application identifiers checked (check digits, dates,
  lengths) and in brackets in the text below the bars
- Every module of a code a whole number of printer dots, with the quiet
  zone its standard asks for, pure black and white, never resampled on printers with
  square dots (except on a colour page too large to send without loss,
  over 8 MB: then JPEG); the VC-500W prints its vivid and colour modes at
  another number of lines per inch along the tape, so there the label is
  resampled along the tape
- Symbol library: over 7000 pictograms (Material Design Icons) found by
  name, alias or category (in English, as the icon set names them),
  recently used ones at hand; drawn as vectors at the printer's resolution,
  in colour, or on black-and-white label printers in black or white with
  sharp edges
- Shipping labels: open a carrier's PDF or image (an A4 page with the label
  in one half or quarter, receipts around it), Tavletta finds the label,
  you check the area, and it is printed at 4 × 6 in, 100 × 150 mm,
  100 × 200 mm, on a 62 mm roll or on A6 or A4 paper; PDF pages are drawn
  at the resolution they are printed at, so barcodes stay sharp (the system
  printer gets them without loss, as PWG Raster at its resolution or as a
  PDF at 300 dpi). The label file keeps the page the label was taken from
- Variables in texts and codes: date with formats and "best before"
  arithmetic, time, a counter that goes up with every printed label (also
  across copies), copy number, random ID and UUID
- Command line: `tavletta print` prints a label file without a window or
  display, with copies, counter start and texts set per job
- Print server: `tavletta serve` offers a REST API (OpenAPI 3.1) and MCP
  for AI assistants (`tavletta mcp` for Claude Desktop and Claude Code),
  with print jobs queued per printer, named printers with their settings
  (`printers.toml`), access tokens with roles; a container image for
  amd64 and arm64 (`ghcr.io/joshii-h/tavletta`), a Docker Compose example
  and a systemd unit
- Serial labels: placeholders such as `{Name}` in texts and codes, filled
  from each row of a CSV, ODS or XLSX file; step through the rows in the
  preview and print all of them, or a range, on any printer
- Live preview at print resolution: what you see is what is printed
- Millimetres or inches, chosen from the system language
- Printer problems explained in plain words, with what to do;
  *Report a problem* prepares an issue with all the details
- Save labels as `.tavletta` files (images and symbols embedded; a data file
  for serial labels is linked, not embedded) and reuse them as templates
- Drag and drop images, data files or `.tavletta` files onto the window
- Export as PNG

What each printer adds is listed under [Supported printers](printers.md).
