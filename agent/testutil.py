"""Test helper: import a server's modules without polluting sys.modules.

The Prometheus and Kubernetes servers each have a `backend.py` and a `tools.py`. That is fine at
run time (each server is its own process), but one test process importing both would get whichever
`backend` Python cached first. server_modules() imports from one server folder in isolation.
"""

import importlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHARED_NAMES = ("backend", "tools", "render")


def server_modules(server: str, *names: str):
    """server_modules("prometheus", "tools", "backend") -> (tools, backend) of mcp_servers/prometheus."""
    saved = {n: sys.modules.pop(n) for n in SHARED_NAMES if n in sys.modules}
    sys.path.insert(0, os.path.join(ROOT, "mcp_servers", server))
    try:
        return tuple(importlib.import_module(n) for n in names)
    finally:
        sys.path.pop(0)
        for n in SHARED_NAMES:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
