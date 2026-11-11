# Etiketto

Etiketto designs labels under Linux and prints them over the network, on
endless tape or on die-cut labels. It talks to each printer in its own
language.

![Main window](media/main.png#only-light)
![Main window](media/main-dark.png#only-dark)

- Text, images, QR codes and barcodes; backgrounds printed edge to edge
- Preview at print resolution: what you see is what is printed
- Endless tape as long as the content, or die-cut labels of any size
- Finds the printer in the network, reads what is loaded and explains
  printer problems in plain words
- 27 languages, follows the light or dark mode of the desktop

The supported printers and what each one can do are listed under
[Printers](printer.md). Further printers can be added as a backend, see
[Contribute](contribute.md).

## Install

### Flatpak

Flathub is in preparation. Until then, build it yourself:

```sh
git clone https://git.joshuahirsig.xyz/joshii/etiketto.git && cd etiketto
flatpak install --user flathub org.gnome.Sdk//51 org.gnome.Platform//51
flatpak-builder --user --install --force-clean build-dir \
    build-aux/flatpak/xyz.joshuahirsig.Etiketto.yml
```

### install.sh

Install the system packages, then run the script as your user (no root):

| Distribution | Packages |
|---|---|
| Debian, Ubuntu | `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-gi-cairo python3-venv gettext` |
| Fedora | `python3-gobject gtk4 libadwaita python3-cairo gettext` |
| Arch | `python-gobject gtk4 libadwaita python-cairo gettext` |
| Gentoo | `dev-python/pygobject gui-libs/gtk:4 gui-libs/libadwaita dev-python/pycairo sys-devel/gettext` |

```sh
git clone https://git.joshuahirsig.xyz/joshii/etiketto.git && cd etiketto
./install.sh
```

Uninstall: `./install.sh --uninstall` or `flatpak uninstall xyz.joshuahirsig.Etiketto`.
Saved labels and templates are kept.

## First label

1. Switch the printer on and choose it under [[ui:Printer model]]. Etiketto
   finds it in the network (Bonjour) and reads what is loaded: the tape
   width or the distance between the labels.
2. Not found? Enter the address under [[ui:Host name or IP address]], e.g.
   `printer.local` or `192.168.1.50`. New printer: connect it to the
   network first, see [Printers](printer.md).
3. [[ui:Add]] text, an image or a code, then [[ui:Print]].
4. [[ui:Save]] stores the label as `.etiketto` file, [[ui:Save as template…]]
   adds it to [[ui:Templates]].
