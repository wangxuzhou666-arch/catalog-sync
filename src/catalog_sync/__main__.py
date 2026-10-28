"""``python -m catalog_sync``: the same as the ``catalog-sync`` command."""

import sys

from catalog_sync.cli import main

sys.exit(main())
