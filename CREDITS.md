# Credits

The Brother VC-500W protocol was reverse engineered by
[m7i.org](https://m7i.org/projects/labelprinter-linux-python-for-vc-500w/);
Tavletta prints through its descendant labelprinter (m7i.org → lenchan139
→ [sgrimee](https://github.com/sgrimee/labelprinter-vc500w)), in a fork
pinned to a reviewed commit until its fixes are merged:
<https://github.com/joshii-h/labelprinter-vc500w>.

The VC-500W on USB follows the observations of
[barnguard](https://github.com/JulianaGaibler/barnguard) (Juliana Gaibler,
MPL-2.0), written anew; no barnguard code is included.

Apple Raster (URF) for AirPrint printers follows the byte layout of
[libcups](https://github.com/OpenPrinting/libcups),
[PAPPL](https://github.com/michaelrsweet/pappl) and
[libcupsfilters](https://github.com/OpenPrinting/libcupsfilters)
(OpenPrinting, Apple Inc., Michael R Sweet, Till Kamppeter; Apache-2.0),
written anew; no code of theirs is included.

The Brother QL raster commands, models and media table follow Brother's
public Raster Command Reference documents and
[brother_ql](https://github.com/pklaus/brother_ql) by Philipp Klaus and
its fork [brother-ql-next](https://github.com/LunarEclipse363/brother_ql)
by LunarEclipse (both GPL-3.0), written anew; no brother_ql code is
included.

Printing on Brother P-touch printers follows Brother's Raster Command
References (PT-E550W/P750W/P710BT, PT-P900 series) with the model list,
USB IDs and quirks documented by
[ptouch-print](https://git.familie-radermacher.ch/linux/ptouch-print.git)
(Dominic Radermacher, GPL-3.0-only) and the command sequence and print
area tables of [ptouch](https://github.com/nbuchwitz/ptouch) (Nicolai
Buchwitz, LGPL-2.1-or-later); written anew from these facts, no code of
either is included.

Printing on cab printers follows cab's public
[JScript Programming Manual](https://www.cab.de/en/programming); no
reverse engineering and no cab code are involved.

Most code types are encoded by [Zint](https://zint.org.uk) (libzint,
Copyright (C) 2009-2025 Robin Stuart and contributors, BSD-3-Clause) through
[zint-bindings](https://github.com/bindreams/zint-bindings) (Anna Zhukova,
Apache-2.0); Tavletta takes Zint's modules and draws them itself. The
Flatpak ships Zint's license as `share/licenses/xyz.joshuahirsig.Tavletta/python3-zint-bindings/LICENSE.zint`.
QR codes come from [segno](https://github.com/heuer/segno) (BSD-3-Clause),
or from Zint when an error correction level is chosen;
Code 128 and EAN-13 from
[python-barcode](https://github.com/WhyNotHugo/python-barcode) (MIT).
Images are handled by [Pillow](https://python-pillow.org) (Secret Labs AB,
Fredrik Lundh, Jeffrey 'Alex' Clark and contributors; MIT-CMU).

The label sheet templates come from the template database of
[gLabels](https://github.com/j-evins/glabels-qt) by Jaye Evins, MIT license,
Copyright (C) 2001-2026 Jaye Evins
([`tavletta/sheets/LICENSE.glabels`](tavletta/sheets/LICENSE.glabels)).

The symbols are the icons of
[Material Design Icons](https://pictogrammers.com/library/mdi/) 7.4.47 by
[Pictogrammers](https://pictogrammers.com) (Austin Andrews and contributors),
Apache License 2.0
([`tavletta/symbols/LICENSE.mdi`](tavletta/symbols/LICENSE.mdi)); some are
based on Google's Material Icons (Apache-2.0). The "Brand / Logo" category
and further icons showing brands or trademarks are left out
([`tools/mdi_symbols.py`](tools/mdi_symbols.py)). Tavletta draws the icons
with its own SVG path parser.

Detecting the label on a carrier's page follows
[ThermalPrint](https://github.com/arios4k/thermalprint) by Alexis
Rios-Negron (MIT License), rewritten in Python; see
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). PDF pages are drawn by
[Poppler](https://poppler.freedesktop.org).

ZPL, EPL2, TSPL, ESC/POS and DYMO LabelWriter follow the byte sequences of
[LPrint](https://github.com/michaelrsweet/lprint) by Michael R Sweet
(Apache-2.0, Copyright 2019-2026 Michael R Sweet), reimplemented in Python;
no LPrint code is included. Where Tavletta differs from LPrint (gap sensor,
EPL2 blank rows, cut commands) and what comes from the vendors' manuals is
marked in the protocol notes in `docs/`.

The NIIMBOT protocol is written anew from the protocol knowledge in
[niimbluelib](https://github.com/MultiMote/niimbluelib) (MultiMote, MIT)
and [Thermalith](https://github.com/EvilGeniusLabs-ca/Thermalith) /
Niimbot.Net (EvilGeniusLabs, GPL-3.0-or-later); no code of either is
included.

The variables (counter, date and UUID in texts and codes) were inspired by
[labelburg](https://github.com/DL6ER/labelburg) (DL6ER, GPL-3.0); ideas
only, no code of it is included.

Everything Tavletta ships or derives from, with licenses and notices, is
listed in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
