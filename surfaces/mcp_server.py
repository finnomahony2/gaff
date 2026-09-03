#!/usr/bin/env python3
"""Thin shim over ``gaff_engine.mcp`` — kept so existing paths stay live.

The MCP server moved INTO the package (BACKLOG R3): ``gaff-mcp`` (the console
script) and ``python3 -m gaff_engine.mcp`` are the primary entry points now.
This file remains because installed host configs and the surface tests point
at it (``spike/`` in the lab, ``surfaces/`` in the assembled package). The
stdout-claim-before-imports guard (E3 rule 1) lives in ``gaff_engine.mcp``
itself and fires when this shim imports it — before ``gaff_engine.tools``
and everything the tools lazily import get a chance to print.
"""
import os
import sys

# The package sits one directory up in both trees (lab and assembled output).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from gaff_engine.mcp import PROTOCOL, handle, main  # noqa: E402,F401

if __name__ == "__main__":
    main()
