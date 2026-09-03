#!/usr/bin/env python3
"""Thin shim over ``gaff_engine.tools`` — kept so existing paths stay live.

The tool layer moved INTO the package (BACKLOG R2): a pip-installed wheel now
carries it, so the skill folder and the MCP server work with no checkout. This
file remains because tests, docs and installed configs import ``gaff_tools``
from this directory (``spike/`` in the lab, ``surfaces/`` in the assembled
package); it re-exports the real module's objects — the SAME objects, not
copies, so monkeypatching ``DISPATCH``/``TOOLS`` here (as the surface tests
do) is seen by both surfaces.
"""
import os
import sys

# The package sits one directory up in both trees (lab and assembled output).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.tools import *          # noqa: E402,F401,F403
from gaff_engine.tools import (          # noqa: E402,F401  (explicit, for greppability)
    DISPATCH, TOOLS, ToolError, UsageError, cli_main, safe_call)
