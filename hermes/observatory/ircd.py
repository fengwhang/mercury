"""Compatibility entry point for the MIRC daemon."""
import runpy
import sys

if __name__ == "__main__":
    runpy.run_module("observatory.mirc", run_name="__main__")
else:
    from . import mirc as _implementation
    sys.modules[__name__] = _implementation
