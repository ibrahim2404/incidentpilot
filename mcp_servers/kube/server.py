"""Read-only Kubernetes MCP server for IncidentPilot (thin adapter over tools.py).

Env:
  READER_KUBECONFIG  kubeconfig of the read-only ServiceAccount (make reader-kubeconfig)
  KUBE_NAMESPACE     namespace to inspect (default shop)
  KUBE_FIXTURE       folder of a recorded incident, for replays (overrides live access)
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))

from mcp.server import MCPServer  # noqa: E402

import tools  # noqa: E402
from backend import from_env  # noqa: E402

mcp = MCPServer("kubernetes")
backend = from_env()


@mcp.tool()
def list_pods() -> str:
    """List every pod of the shop with its app, phase, ready containers, restart count,
    current state (e.g. CrashLoopBackOff) and last termination (e.g. OOMKilled, exit code).
    Use it early to spot pods that are missing, not ready or restarting."""
    return tools.list_pods(backend)


@mcp.tool()
def describe_workload(name: str) -> str:
    """Describe one deployment: desired vs ready replicas, rollout conditions, last restart,
    container image, resource limits, the configmap it reads, and its recent revisions.
    Use it to check a suspected service, e.g. a rollout stuck on a new revision or 0 replicas.
    name is the deployment name, like "inventory"."""
    return tools.describe_workload(backend, name)


@mcp.tool()
def get_events(minutes: int = 30) -> str:
    """Recent Kubernetes events in the shop namespace, newest first (BackOff, Killing,
    OOMKilling, FailedScheduling, ScalingReplicaSet, Unhealthy...). minutes = how far back (1 to 180).
    Use it to learn what Kubernetes itself did or saw: restarts, scaling, failed probes."""
    return tools.get_events(backend, minutes)


@mcp.tool()
def get_config(name: str) -> str:
    """Show the settings of a configmap and how long ago it was last modified.
    Each service reads <service>-config (e.g. "orders-config").
    Use it when a recent change is suspected: a wrong address, a flag, a typo."""
    return tools.get_config(backend, name)


if __name__ == "__main__":
    mcp.run()
