# Third-party notices

Tavletta is licensed under the AGPL-3.0-or-later. This is the complete list
of what it ships or derives from, with the licenses and notices. Section 1
is code and data that are part of Tavletta or its Flatpak; section 2 the
sources of protocol knowledge, used as facts only: no code of theirs is
included, the protocols were written anew.

In the Flatpak each component's licence is in
`share/licenses/xyz.joshuahirsig.Tavletta/<module>/` (this file in
`tavletta/`).

## Licence expressions

- The Python package (sdist and wheel, `pyproject.toml`): what it contains,
  Tavletta's code and the data and code derived from others:
  `AGPL-3.0-or-later AND MIT AND Apache-2.0`.
- The app as the Flatpak ships it (the metainfo's `project_license`): the
  package and the libraries the Flatpak bundles for it:
  `AGPL-3.0-or-later AND MIT AND Apache-2.0 AND BSD-3-Clause AND MIT-CMU AND (GPL-2.0-only OR GPL-3.0-only)`.

## 1. Shipped or derived

### ThermalPrint (MIT)

The label detection for shipping labels (`tavletta/shipping/detect.py`:
cutting the page at blank rows and columns, choosing the block most like a
label, the test for linear barcodes, the margin kept around the label)
follows the ideas and algorithm of ThermalPrint's `src/detection.rs`,
rewritten in Python: <https://github.com/arios4k/thermalprint>

Portions derived from ThermalPrint, Copyright (c) 2026 Alexis Rios-Negron,
MIT License:

```text
MIT License

Copyright (c) 2026 Alexis Rios-Negron

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### labelprinter (AGPL-3.0-or-later)

The VC-500W connection and the parsing of its answers come from
labelprinter, a dependency (the fork
<https://github.com/joshii-h/labelprinter-vc500w>, from
<https://github.com/sgrimee/labelprinter-vc500w>; m7i.org → lenchan139 →
sgrimee). Copyright (c) 2021 Andrea Micheloni and contributors, GNU Affero
General Public License version 3 or later, the license of Tavletta itself.
The protocol was reverse engineered by m7i.org
(<https://m7i.org/projects/labelprinter-linux-python-for-vc-500w/>).

### gLabels templates (MIT)

The label sheets (`tavletta/sheets/templates.json`) are converted from the
template database of gLabels (<https://github.com/j-evins/glabels-qt>),
Copyright (C) 2001-2026 Jaye Evins, MIT license. The full license text is
in [`tavletta/sheets/LICENSE.glabels`](tavletta/sheets/LICENSE.glabels).

### Material Design Icons (Apache-2.0)

The symbols (`tavletta/symbols/mdi.json.gz`) are the icons of Material
Design Icons 7.4.47 by Pictogrammers (Austin Andrews and contributors,
<https://pictogrammers.com>), Apache License 2.0; some are based on
Google's Material Icons (Apache-2.0). Notice and license text:
[`tavletta/symbols/LICENSE.mdi`](tavletta/symbols/LICENSE.mdi).

### Zint (BSD-3-Clause)

libzint 2.15.0 (<https://zint.org.uk>), Copyright (C) 2009-2025 Robin
Stuart and contributors, BSD-3-Clause, encodes most code types. It comes
inside zint-bindings; its license text is in
[`build-aux/flatpak/LICENSE.zint`](build-aux/flatpak/LICENSE.zint) (in the
source distribution too), and the Flatpak installs it as
`share/licenses/xyz.joshuahirsig.Tavletta/python3-zint-bindings/LICENSE.zint`.

### zint-bindings (Apache-2.0)

The Python bindings for libzint (<https://github.com/bindreams/zint-bindings>),
Copyright 2024-2025 Anna Zhukova, Apache License 2.0; a dependency, the
Flatpak bundles its wheel.

### segno (BSD-3-Clause)

QR codes (<https://github.com/heuer/segno>), Copyright (c) 2016-2025 Lars
Heuer, BSD-3-Clause; a dependency, bundled in the Flatpak.

### python-barcode (MIT)

Code 128 and EAN-13 (<https://github.com/WhyNotHugo/python-barcode>),
Copyright (c) 2010-2013 Thorsten Weimann, (c) 2017-2023 Hugo Osvaldo
Barrera et al., MIT license; a dependency, bundled in the Flatpak.

### Pillow (MIT-CMU)

Image handling (<https://python-pillow.org>), Copyright (c) 1997-2011 by
Secret Labs AB, (c) 1995-2011 by Fredrik Lundh and contributors, (c)
2010 by Jeffrey 'Alex' Clark and contributors, MIT-CMU (HPND) license; a
dependency, bundled in the Flatpak.

### Poppler and poppler-data (Flatpak only)

The Flatpak bundles Poppler's GLib library to draw PDF pages:
<https://poppler.freedesktop.org>. Poppler is a fork of Xpdf, which is
licensed under the GNU General Public License version 2 or version 3, not
any later version (GPL-2.0-only OR GPL-3.0-only); Poppler's own additions
are GPL-2.0-or-later. Both licence texts are in its source (`COPYING`,
`COPYING3`).

It also bundles poppler-data (encoding files for Chinese, Japanese, Korean
and Cyrillic text in PDF files): <https://poppler.freedesktop.org>. Its
CMap files are under the Adobe licence in `COPYING.adobe` of its source
(BSD-3-Clause style, Copyright 1990-2019 Adobe); the cidToUnicode,
nameToUnicode and unicodeMap files are Copyright 1996-2011 Glyph & Cog, LLC, under
GPL-2.0-only OR GPL-3.0-only; its Makefile is under the MIT licence.

Outside the Flatpak, Poppler comes from the distribution.

## 2. Protocol knowledge (facts only, no code included)

- **LPrint** (<https://github.com/michaelrsweet/lprint>), Michael R Sweet,
  Apache-2.0: the byte sequences of ZPL, EPL2, TSPL, ESC/POS and DYMO
  LabelWriter; differences and what comes from the vendors' manuals are
  marked in `docs/`.
- **libcups** (<https://github.com/OpenPrinting/libcups>), **PAPPL**
  (<https://github.com/michaelrsweet/pappl>) and **libcupsfilters**
  (<https://github.com/OpenPrinting/libcupsfilters>), OpenPrinting, Apple
  Inc., Michael R Sweet, Till Kamppeter, Apache-2.0: the byte layout of
  Apple Raster (URF). PWG Raster and IPP follow their public standards (PWG
  5102.4, RFC 8011, PWG 5100.x).
- **brother_ql** (<https://github.com/pklaus/brother_ql>), Philipp Klaus,
  and its fork **brother-ql-next**
  (<https://github.com/LunarEclipse363/brother_ql>), LunarEclipse, both
  GPL-3.0: Brother QL models and media table.
- **ptouch-print** (<https://git.familie-radermacher.ch/linux/ptouch-print.git>),
  Dominic Radermacher, GPL-3.0-only: the P-touch model list, USB IDs and
  quirks, used as facts and tables only.
- **ptouch** (<https://github.com/nbuchwitz/ptouch>), Nicolai Buchwitz,
  LGPL-2.1-or-later: the P-touch command sequence and print area tables.
- **niimbluelib** (<https://github.com/MultiMote/niimbluelib>), MultiMote,
  MIT, and **Thermalith** / Niimbot.Net
  (<https://github.com/EvilGeniusLabs-ca/Thermalith>), EvilGeniusLabs,
  GPL-3.0-or-later: the NIIMBOT packet protocol.
- **barnguard** (<https://github.com/JulianaGaibler/barnguard>), Juliana
  Gaibler, MPL-2.0: observations of the VC-500W on USB.
- **Brother's Raster Command References** (QL series; PT-E550W/P750W/P710BT,
  PT-P900 series): Brother's public developer documents for the QL and
  P-touch backends.
- **The printer makers' manuals**: Zebra's "ZPL II Programming Guide" and
  "EPL2 Programming Guide", TSC's "TSPL/TSPL2 Programming Manual", Epson's
  "ESC/POS Command Reference" and DYMO's "LabelWriter 450 Series Technical
  Reference Manual"; what Tavletta takes from them is marked [spec] in
  `docs/`.
- **Brother's Android app "Color Label Editor 2"** 1.1.6: an
  interoperability analysis for the VC-500W (permitted under Swiss URG
  Art. 21 / EU Directive 2009/24/EC Art. 6), our own description in
  `docs/vc500w.md`; no Brother code is included.
- **m7i.org**'s reverse engineering of the VC-500W
  (<https://m7i.org/projects/labelprinter-linux-python-for-vc-500w/>).
- **cab's JScript Programming Manual** (<https://www.cab.de/en/programming>):
  the cab backend; no reverse engineering, no cab code.
- **labelburg** (<https://github.com/DL6ER/labelburg>), DL6ER, GPL-3.0:
  inspiration for the variables (counter, date and UUID templates); ideas
  only.
