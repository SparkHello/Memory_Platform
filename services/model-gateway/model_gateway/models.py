"""Compatibility imports for the standalone Model Gateway contracts package.

New code should import configuration types from :mod:`model_gateway_contracts`.
This module intentionally contains no subclasses or wrappers: old imports and
new imports resolve to the exact same Python objects.
"""

from model_gateway_contracts.models import *  # noqa: F403
from model_gateway_contracts.models import __all__
