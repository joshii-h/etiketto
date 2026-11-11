# Contribute

Code, bug reports, translations and tests with other printers, cassettes and
label rolls are welcome.

Issues and pull requests on [GitHub](https://github.com/joshii-h/etiketto) are
welcome, no Forgejo account needed. They are copied automatically to
[Forgejo](https://git.joshuahirsig.xyz/joshii/etiketto), the main repository,
with a link on GitHub. Review and merge happen there; your commits are kept,
so GitHub shows the pull request as merged. The discussion can stay on GitHub.
With a Forgejo account you can contribute there directly.
Details: [CONTRIBUTING.md](https://git.joshuahirsig.xyz/joshii/etiketto/src/branch/main/CONTRIBUTING.md).

- **App translations**: `tools/strings_<lang>.py`, see CONTRIBUTING.
- **This documentation**: `site/po/<lang>.po`, see
  [site/README.md](https://git.joshuahirsig.xyz/joshii/etiketto/src/branch/main/site/README.md).
  Names of buttons and settings come from the app translation automatically.
- **Other printers**: printing goes through a backend interface; a new printer
  is a new backend. The protocols of the supported printers are described in
  [docs/protocol.md](https://git.joshuahirsig.xyz/joshii/etiketto/src/branch/main/docs/protocol.md)
  (Brother VC-500W) and
  [docs/cab.md](https://git.joshuahirsig.xyz/joshii/etiketto/src/branch/main/docs/cab.md)
  (cab).
