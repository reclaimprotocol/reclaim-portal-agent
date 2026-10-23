"""Deprecated import shim for the retired OpenRouter module name.

The implementation uses the direct OpenAI API and lives in
``agent.openai_cascade``. Keep this shim temporarily so external scripts do
not break during migration; it contains no OpenRouter client or endpoint.
"""

from agent.openai_cascade import *  # noqa: F401,F403
