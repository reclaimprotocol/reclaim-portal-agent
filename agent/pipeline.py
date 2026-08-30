"""Compatibility shim — this module now lives in `agent.legacy.pipeline`.

Kept at its original path so the ~111 scripts and the production `genie/` app
keep importing `agent.pipeline` unchanged.

The alias below rebinds this module's entry in sys.modules to the real module
object, so `agent.pipeline is agent.legacy.pipeline`. Re-exporting names instead would
create a SECOND module instance, and the module-level caches, locks and
`_PARENT_PORTAL_CACHE`-style state in the V2 engine would silently diverge
between the two copies.
"""
import sys as _sys

from agent.legacy import pipeline as _target

_sys.modules[__name__] = _target
