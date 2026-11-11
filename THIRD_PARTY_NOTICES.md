# Third-party notices

Etiketto is licensed under the AGPL-3.0-or-later. Parts of it are derived
from software under other licenses, listed here with their notices.

## ThermalPrint

The label detection for shipping labels (`etiketto/shipping/detect.py`:
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

## Poppler (Flatpak only)

The Flatpak bundles Poppler's GLib library to draw PDF pages:
<https://poppler.freedesktop.org>, GPL-2.0-or-later. Outside the Flatpak,
Poppler comes from the distribution.
