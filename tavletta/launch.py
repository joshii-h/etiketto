"""The `tavletta` command, kept light until it knows what it runs.

`tavletta serve` ignores SIGHUP from its very first line: systemd's
`reload` (or anyone) sending it while Python still imports would end the
process, as SIGHUP does by default. The server installs its real handler
(read tokens.toml again) before it reads its files.
"""

from __future__ import annotations

import signal
import sys


def main() -> int:
    if sys.argv[1:2] == ["serve"]:
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    from tavletta.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
