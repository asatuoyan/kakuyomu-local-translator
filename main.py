"""Compatibility entry point; implementation lives in translator.engine."""
import sys
import translator.engine as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
