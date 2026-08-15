"""``python -m redpymake`` 入口，转发到 :func:`redpymake._cli.main`。"""

from __future__ import annotations

import sys

from ._cli import main


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
