# Serial labels

One label for each row of a table: inventory numbers, shelf labels, name
badges, addresses. The label holds placeholders such as `{Name}`; each
printed label shows the values of its row.

![Serial labels](media/serial.png#only-light)
![Serial labels](media/serial-dark.png#only-dark)

## Data file

- [[ui:Data for serial labels…]] in the main menu, [[ui:Choose…]] under
  [[ui:Serial labels]], or drop the file onto the window.
- CSV (comma, semicolon or tab), ODS (LibreOffice) or XLSX (Excel). The
  first filled row holds the column names.
- A title above the column names counts as the first row: delete it, or
  put the table on a sheet of its own.
- ODS: cells as LibreOffice shows them, dates as `2026-11-11`.
- XLSX: numbers with their decimals, leading zeros and percent signs,
  dates as `2026-11-11`. Thousands separators and text in the number
  format, such as a currency, are left out. Dates in the East Asian
  built-in formats come out as numbers.
- Empty rows are left out. The hidden cells of merged cells are empty.
- Several sheets: choose one under [[ui:Sheet]].
- [[ui:Read again]] after the file was changed in the spreadsheet program.
- Up to 100 000 rows, 1000 columns, 2 000 000 cells (rows times
  columns, without the row of column names) and 50 MB.

The label file stores where the data file is, never its content. If the
data file lies in the same folder as the label (or below it), both can be
moved or shared together. If it is missing when the label is opened,
Etiketto says so: choose it again or remove it.

A data file is read without asking only if it is a table (`.csv`, `.tsv`,
`.txt`, `.ods`, `.xlsx`) in the label's folder or below, and not in a
hidden folder. Any other data file is read only after asking, once for
each label file: a label from someone else could name any file.
[[ui:Forget allowed data files]] in the main menu makes Etiketto ask again. Templates
keep the placeholders but not the data file. Older versions of Etiketto refuse labels with a
data file instead of printing the placeholders.

## Placeholders

- [[ui:Insert column]] next to a text or the content of a code adds
  `{Column}` at the cursor; typing it works as well.
- Placeholders work in texts and in QR codes and barcodes, for example
  `https://example.org/item/{Number}`.
- `{{` and `}}` print a single brace in a text with placeholders; in a
  text without any they stay as they are. So `{{Name}}` alone prints
  `{{Name}}`, and `{{Name}} {Name}` prints `{Name} Anna`.
- Values are printed as they are, even if they contain braces.
- A placeholder without a column of that name is printed as it is; the
  line under the preview names it.
- Before printing, the codes of every row are checked: an EAN-13 needs
  12 or 13 digits, an empty cell gives no code. Rows that fail are named;
  print without them or correct the data file.

- Variables such as `{=counter}` or `{=date}` work together with the
  placeholders; the counter goes on from row to row.

## Preview

- The buttons under the preview step through the rows: [[ui:First row]],
  [[ui:Previous row]], [[ui:Next row]] and [[ui:Last row]], or
  Alt+Page Up, Alt+Page Down, Alt+Home and Alt+End.
- Rows are counted without the row of column names.
- [[ui:Export PNG…]] saves the row that is shown.

## Print

- [[ui:Print]] prints one label for each row. [[ui:From row]] and
  [[ui:To row]] limit the rows (all of them by default),
  [[ui:Copies of each label]] sets how many of each are printed.
- Every label is a print job of its own, so this works on every printer.
- [[ui:Stop]] in the bar above the window ends the series after the label
  being printed; the first label is always printed.
- If the printer reports a problem, printing stops at that row. Solve the
  problem, then print again starting from that row.

## Command line

`etiketto print shelf.etiketto --data items.csv --rows 3-10` prints rows 3
to 10 without opening a window; see [Command line](use.md#command-line).
With `--dry-run` each line of the listing starts with the row number.
