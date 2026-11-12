# Variables and command line

Texts and code contents can contain variables, filled in when printing:

| Variable | Result |
|---|---|
| `{=date}`, `{=date:%d.%m.%Y}` | today, in the system language's or a strftime format |
| `{=date+30d}` | in 30 days; units `d`, `w`, `m`, `y`, `h`, combined like `+1m-1d` |
| `{=time}`, `{=time:%H:%M}` | the time |
| `{=counter}`, `{=counter:0001}` | running number, optionally with leading zeros |
| `{=copy}`, `{=copies}` | number of this copy, of all copies |
| `{=id}`, `{=id:12}`, `{=uuid}` | short random ID (Crockford base 32), UUID 4 |
| `{=NAME}` | a value given on the command line (letters of any language, digits, `_`) |

The counter goes up by its step with every printed label, also across
copies, and the label file keeps the next number. All variables of one
label have one value, so a counter in the text and in the QR code match.
`{{` and `}}` are literal braces; a `{=` that is not closed is reported.
Dates are in the system's time zone: `+1d` keeps the time of day across a
change to summer time, `+2h` counts real hours, `%z`/`%Z` work. A label
that uses variables or a counter is saved as format 2, which older
Tavletta versions refuse instead of printing `{=date}` as it is. The `=` keeps variables apart from the
`{Column}` placeholders of serial labels: a column named "Date" never
changes what `{=date}` means.

```sh
tavletta print FILE [--printer ID] [--host ADDR] [--copies N]
                    [--set NAME=VALUE ...] [--set-literal NAME=VALUE ...]
                    [--counter-start N] [--no-save-counter] [--no-query]
                    [--printer-defaults] [--print-method printer|transfer|direct]
                    [--data FILE [--sheet NAME] [--rows FROM-TO]]
                    [--dry-run [--output out.png]]
tavletta list-printers [--printer ID]
tavletta serve [--host ADDR] [--port N] [--labels-dir DIR] [--mcp] [--discover]
               [--printers FILE] [--tokens FILE | --token-file FILE]
tavletta mcp [--labels-dir DIR] [--printers FILE]
tavletta token new NAME [--role preview|print|admin] [--tokens FILE]
tavletta --version | --help
```

`tavletta print` needs no display. `--set` replaces the text of the
element with that name (set in the element's settings) or fills in
`{=NAME}`; variables in a new text are filled in (`--set-literal` takes the
value as it is). `--set date=2026-12-31` prints with another date.
`--no-save-counter` prints a read-only label without saving the counter
(the next print repeats the numbers). Without
`--host` the printer set in the app is used, or the only one found.
It prints with the choices last made in the app for that printer model:
cut, colours, quality, the P-touch's chain printing, high resolution and
tape feed, the printer settings (label sensor, darkness …) and whether a
label sheet gets outlines; `--printer-defaults` takes the model's defaults
for these instead (no outlines; the printer's address and the safety
margin stay as set in the app).
`--print-method` sets the print method of a thermal printer that has the
setting (cab) for this print: `printer` (as set on the printer),
`transfer` (with a ribbon) or `direct`; it applies with
`--printer-defaults` too.
A die-cut roll whose measured label distance (cab) is not the label's
height plus gap gets a hint on standard error, as the window warns.
Printers whose label depends on what they report are asked first, for at
most 5 s (P-touch: model and tape; NIIMBOT: model, label types and density
range; cab, ZPL, EPL2, TSPL, ESC/POS, DYMO and office printers: the resolution; a dry run waits
2 s); the label is then checked for that printer. A question still on its
way after that gets up to 10 s more to end before printing (Tavletta says
so; an answer in that time is used); what it brings later still sets the
resolution the label is drawn at.
`--no-query` skips the question. A label set up for a label sheet prints on it as in the
window: each copy (with `--data`, each copy of each row) at the next of
the chosen positions.
`--data` prints serial labels: one for each row of a CSV, ODS or XLSX file
(by default the label's own data file, if it lies beside it), `--sheet`
and `--rows 3-10` choose which; the counter goes on from row to row.
`--dry-run` lists the texts of every label and draws the first as the
printer would get it; with `--output` it saves each label as the
window's export does: as printed, on the whole tape, at most 600 dpi
like the preview (a large page coarser), with its resolution in the PNG
(`-`: the first on standard output). Every label it saves is checked
too: a code that does not fit stops it (exit 4); without `--output` the
labels after the first are not drawn, so only the rows' columns and
codes are checked.
The label file is locked while printing, so two prints of one file never
share a number; the window reads the saved counter again before printing
and saving. The counter is saved also after a printer error, Ctrl+C,
SIGTERM or SIGHUP. Exit codes: 0 printed, 1 printer problem, 2 wrong
arguments, 3 file problem (also still locked after 60 s) or output not
written, 4 label cannot be printed (empty, unknown or unclosed variable,
date out of range, unknown column, a code a row cannot make), 5 the counter cannot be saved (checked before
printing; or printed, but saving failed), 130 interrupted.

In the Flatpak, hand the files over with `--file-forwarding`: the label
and a data file, each between `@@`; the label's own data file is only
reached if it is handed over too:

```sh
flatpak run --file-forwarding xyz.joshuahirsig.Tavletta print @@ label.tavletta @@
flatpak run --file-forwarding xyz.joshuahirsig.Tavletta print @@ label.tavletta @@ --data @@ rows.csv @@
```
