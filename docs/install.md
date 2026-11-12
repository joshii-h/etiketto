# Install

Ways to install Tavletta 1.1:

- **Flatpak bundle**: `Tavletta-v1.1.0-x86_64.flatpak` from the
  [release](https://git.joshuahirsig.xyz/joshii/tavletta/releases/tag/v1.1.0),
  then `flatpak install --user Tavletta-v1.1.0-x86_64.flatpak`. Flathub is
  in preparation; to build the Flatpak yourself see
  [below](#flatpak-build-locally).
- **Print server as a container**: `docker pull ghcr.io/joshii-h/tavletta:1.1`
  (amd64 and arm64), see [server.md](server.md#container) and the
  [server installation guide](https://tavletta.joshuahirsig.xyz/server-install/).
- **pip** (the system packages below are needed too):
  `pip install "git+https://git.joshuahirsig.xyz/joshii/tavletta.git@v1.1.0"`
- **install.sh**: per user, from a clone, as follows.

## install.sh

Requirements from your distribution: Python 3.11+, PyGObject with GTK 4
and libadwaita, pycairo, gettext. Everything else goes into a private
virtual environment.

```sh
git clone https://git.joshuahirsig.xyz/joshii/tavletta.git
cd tavletta
./install.sh                # per user, no root; ./install.sh --uninstall
```

| Distribution | system packages |
|---|---|
| Debian / Ubuntu | `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 python3-gi-cairo python3-venv gettext` |
| Fedora | `python3-gobject gtk4 libadwaita python3-cairo gettext` |
| Arch | `python-gobject gtk4 libadwaita python-cairo gettext` |
| Gentoo | `dev-python/pygobject gui-libs/gtk:4 gui-libs/libadwaita dev-python/pycairo sys-devel/gettext` |

For shipping labels from PDF files also Poppler's GLib library with GObject
introspection (images work without it; the Flatpak brings it along):
`gir1.2-poppler-0.18` (Debian, Ubuntu), `poppler-glib` (Fedora, Arch),
`app-text/poppler` with `USE="cairo introspection"` (Gentoo).

## First start

Start **Tavletta** from the app menu and choose the **Printer model**.
Tavletta looks for the printer on the network; otherwise enter its host
name, e.g. `VC-500W1234.local` or `EOS2.local` (needs mDNS / nss-mdns), or
its IP address. For the VC-500W, outside Flatpak, the host is also read from
`~/.config/labelprinter/config.json` if you use the `labelprinter` CLI.
In Flatpak, `.local` names are resolved through the system's Avahi daemon.

## Flatpak (build locally)

```sh
flatpak install --user flathub org.gnome.Sdk//51 org.gnome.Platform//51
flatpak-builder --user --install --force-clean build-dir \
    build-aux/flatpak/xyz.joshuahirsig.Tavletta.yml
flatpak run xyz.joshuahirsig.Tavletta
```

Check it like Flathub does:

```sh
flatpak install --user flathub org.flatpak.Builder
flatpak run --command=flatpak-builder-lint org.flatpak.Builder \
    manifest build-aux/flatpak/xyz.joshuahirsig.Tavletta.yml
```
