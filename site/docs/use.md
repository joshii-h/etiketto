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
variables print exactly as typed.

## Command line { #command-line }

`etiketto print` prints a saved label without opening a window, also on a
computer without a screen:

```sh
etiketto print label.etiketto
etiketto print label.etiketto --copies 5 --counter-start 100
etiketto print label.etiketto --set title="Batch 7" --set lot=A12
etiketto print label.etiketto --printer system --host ipp://printer.local/ipp/print
etiketto print label.etiketto --copies 3 --dry-run --output preview.png
etiketto list-printers
etiketto --version
```

- Without `--host` Etiketto uses the printer set in the app, or the only
  one it finds on the network. `etiketto list-printers` shows model,
  address and name of each printer found.
- `--set NAME=VALUE` replaces the text of the element with that
  [[ui:Name for the command line]] (in the element's settings), or fills in
  a variable of your own: `{=lot}` in the label, `--set lot=A12` when
  printing. `--set date=2026-12-31` prints with another date.
- With a counter the next number is saved in the label file after printing.
- `--dry-run` only checks and renders: it lists the texts of each label;
  `--output file.png` saves the images as the printer would get them
  (`file-1.png`, `file-2.png` … if they differ; `-` writes the first to
  standard output).

Exit codes: 0 printed, 1 printer problem or no printer found, 2 wrong
arguments, 3 label file cannot be read or output not written, 4 the label
cannot be printed (empty, unknown variable), 5 printed, but the counter
could not be saved.

In the Flatpak the label file has to be handed over with
`--file-forwarding`; write images to standard output:

```sh
flatpak run --file-forwarding xyz.joshuahirsig.Etiketto print @@ label.etiketto @@
flatpak run --file-forwarding xyz.joshuahirsig.Etiketto print --dry-run --output - @@ label.etiketto @@ > preview.png
```
