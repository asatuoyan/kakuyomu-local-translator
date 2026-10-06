"""Compatibility entry point; implementation lives in translator.ui.gui."""
import sys
import translator.ui.gui as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
