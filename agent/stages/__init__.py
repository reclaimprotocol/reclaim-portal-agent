"""Compatibility shim — this package now lives in `agent.legacy.stages`.

Aliasing the package object means submodule imports (`agent.stages.discovery`,
`agent.stages.js_renderer`, …) resolve through the legacy package's `__path__`,
so no per-module shim is required — Python's import machinery reads `__path__`
from whatever object sits in sys.modules under the parent name.
"""
import sys as _sys

from agent.legacy import stages as _target

_sys.modules[__name__] = _target
