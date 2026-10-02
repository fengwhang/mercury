"""Compatibility import for the MIRC adapter."""
import sys
from ..mirc import adapter as _implementation
sys.modules[__name__] = _implementation
