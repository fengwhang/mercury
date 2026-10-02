"""Compatibility import for the renamed Mercury fork module."""
import sys
from . import mlounge_share_tool as _implementation
sys.modules[__name__] = _implementation
