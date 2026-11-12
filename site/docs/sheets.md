# Label sheets { #sheets }

With the [[ui:System printer (CUPS, IPP)]], an office printer prints on
sheets of self-adhesive labels: A4 or US Letter pages with a grid of labels,
as Avery, Zweckform, Herma and many others sell them. Etiketto knows more
than 1700 of these sheets, from the template collection of gLabels.

## Choose the sheet { #choose }

1. [[ui:Printer model]]: [[ui:System printer (CUPS, IPP)]].
2. [[ui:Print on]]: [[ui:Label sheet]]. The list of sheets opens.
3. Search by brand and part number as printed on the package
   ("Avery L7160", "Zweckform 3475"), or by the size of one label in
   millimetres ("70x37", either way round). The paper size narrows the
   list down.
4. The picture shows the sheet; [[ui:Use sheet]] takes it.

The label now has the size of the sheet's labels. Round labels and CDs are
designed as a rectangle, and printed only inside their shape; the preview
shows them that way.

- [[ui:Turn the content]]: for labels read from the side, such as folder
  spines: the label is designed lying on its side and turned onto the
  sheet.
- [[ui:Print on]]: [[ui:Single label]] prints the label as a page of its
  own again; choosing [[ui:Label sheet]] again brings back the last sheet.

## Partly used sheets { #positions }

[[ui:Positions]] shows the sheet. Click a label to leave it out (it is
already used) or to print on it again, or set [[ui:Start at position]]: the
labels are counted row by row from the top left. This only applies to the
first sheet; further sheets are filled completely. The sheet and the
positions are saved with the label.

## Print { #print }

- [[ui:Labels]]: how many labels to print. At first as many as positions
  are chosen; more fill further sheets. The dialog shows how many sheets
  that takes.
- [[ui:Print outlines]]: prints the outline of every label. Print a test
  page on plain paper first and hold it against a label sheet in front of a
  light: if the content sits inside the outlines, the sheet fits.
- Office printers cannot print near the paper's edges. Etiketto asks the
  printer for that margin and warns if the content of a label (inside the
  [[ui:Safety margin]]) reaches into it. Leave those positions out, or
  increase the [[ui:Safety margin]].
- The paper size comes from the sheet; turn off "fit to page" in the
  printer's setup, otherwise the printer may scale the sheet.

The sheet templates come from gLabels (© Jaye Evins, MIT license).
