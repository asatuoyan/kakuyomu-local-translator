"""Compatibility entry point; CLI lives in translator.ui.cli; imports expose translator.engine."""
import sys
import translator.engine as _implementation


if __name__ == "__main__":
    from translator.ui.cli import main
    raise SystemExit(main())
else:
    sys.modules[__name__] = _implementation
