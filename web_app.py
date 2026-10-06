"""Compatibility entry point; implementation lives in translator.ui.web_app."""
import sys
import translator.ui.web_app as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
