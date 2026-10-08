"""Read-only access to the Kubernetes API for the kubernetes and logs MCP servers.

LiveBackend shells out to kubectl with a dedicated read-only kubeconfig (see
deploy/agent/reader-rbac.yaml), so even a bug here cannot change the cluster:
the API server itself refuses every write.
FixtureBackend reads the same data from a folder recorded during an incident.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime
from typing import Protocol

KINDS = {"pods", "deployments", "replicasets", "events", "configmaps", "services", "endpoints"}


class KubeError(Exception):
    """kubectl failed or the data is unavailable. The message is shown to the model."""


class Backend(Protocol):
    def now(self) -> float: ...
    def items(self, kind: str) -> list[dict]: ...
    def logs(self, pod: str, previous: bool, since_s: int, tail: int) -> str: ...


def parse_time(text: str | None) -> float | None:
    """'2026-10-08T12:00:00Z' -> Unix seconds."""
    if not text:
        return None
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


class LiveBackend:
    def __init__(self, namespace: str = "shop", kubeconfig: str | None = None, timeout_s: float = 15.0):
        self.namespace = namespace
        self.kubeconfig = kubeconfig
        self.timeout_s = timeout_s

    def now(self) -> float:
        return time.time()

    def _kubectl(self, *args: str) -> str:
        cmd = ["kubectl"]
        if self.kubeconfig:
            cmd += ["--kubeconfig", self.kubeconfig]
        cmd += ["-n", self.namespace, *args]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            raise KubeError(f"kubectl timed out after {self.timeout_s}s") from None
        if r.returncode != 0:
            raise KubeError(r.stderr.strip()[:300] or f"kubectl exited with {r.returncode}")
        return r.stdout

    def items(self, kind: str) -> list[dict]:
        if kind not in KINDS:
            raise KubeError(f"kind {kind!r} is not allowed")
        return json.loads(self._kubectl("get", kind, "-o", "json"))["items"]

    def logs(self, pod: str, previous: bool, since_s: int, tail: int) -> str:
        args = ["logs", pod, f"--since={since_s}s", f"--tail={tail}"]
        if previous:
            args.append("--previous")
        return self._kubectl(*args)


class FixtureBackend:
    """Replays a recorded incident: <dir>/meta.json, <dir>/<kind>.json, <dir>/logs/<pod>[.previous].log"""

    def __init__(self, directory: str):
        self.directory = directory
        with open(os.path.join(directory, "meta.json")) as f:
            self.recorded_at = float(json.load(f)["recorded_at"])

    def now(self) -> float:
        return self.recorded_at

    def items(self, kind: str) -> list[dict]:
        if kind not in KINDS:
            raise KubeError(f"kind {kind!r} is not allowed")
        path = os.path.join(self.directory, f"{kind}.json")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return json.load(f)["items"]

    def logs(self, pod: str, previous: bool, since_s: int, tail: int) -> str:
        name = f"{pod}.previous.log" if previous else f"{pod}.log"
        path = os.path.join(self.directory, "logs", name)
        if not os.path.exists(path):
            raise KubeError(f"no {'previous ' if previous else ''}logs for pod {pod}")
        with open(path) as f:
            return "".join(f.readlines()[-tail:])


def from_env() -> Backend:
    """KUBE_FIXTURE=<dir> replays a recording; otherwise live kubectl with READER_KUBECONFIG."""
    fixture = os.environ.get("KUBE_FIXTURE")
    if fixture:
        return FixtureBackend(fixture)
    return LiveBackend(os.environ.get("KUBE_NAMESPACE", "shop"), os.environ.get("READER_KUBECONFIG"))
