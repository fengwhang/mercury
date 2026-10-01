"""Compatibility import for the renamed Mercury fork module."""
import sys
from . import mlounge as _implementation
sys.modules[__name__] = _implementation
