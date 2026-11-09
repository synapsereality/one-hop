"""one-hop: check that every old URL redirects to its new URL in exactly one hop."""

from .core import Checker, Hop, Result, Rule, normalise, read_rules

__version__ = "0.2.0"
__all__ = ["Checker", "Hop", "Result", "Rule", "__version__", "normalise", "read_rules"]
