"""Bring a recorded incident back to life for the MCP servers, without any cluster.

  with Replay("golden/S002-20261019T163900Z") as env:   # env: PROM_URL, PROM_AT, KUBE_FIXTURE
      ...start the MCP servers with env...

Metrics: promtool rebuilds a TSDB block from metrics.om.gz (cached in <incident>/.tsdb), and a
private Prometheus serves it on 127.0.0.1 with no scrape jobs and no rules, so nothing changes the
recorded data (the recorded ALERTS series is what list_alerts reads). PROM_AT pins "now" to the
recording time. Kubernetes: KUBE_FIXTURE points the kube tools at the recorded folder.

Binaries: PROMETHEUS_BIN / PROMTOOL_BIN, else .bin/ (make replay-tools), else PATH.
"""

from __future__ import annotations

import gzip
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _binary(name: str) -> str:
    env = os.environ.get(f"{name.upper()}_BIN")
    local = os.path.join(ROOT, ".bin", name)
    found = env or (local if os.path.exists(local) else shutil.which(name))
    if not found:
        raise SystemExit(f"{name} not found: run `make replay-tools` or set {name.upper()}_BIN")
    return found


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_tsdb(incident_dir: str) -> str:
    tsdb = os.path.join(incident_dir, ".tsdb")
    if os.path.isdir(tsdb) and os.listdir(tsdb):
        return tsdb
    with tempfile.TemporaryDirectory() as tmp:
        text = os.path.join(tmp, "metrics.om")
        with gzip.open(os.path.join(incident_dir, "metrics.om.gz"), "rb") as src, open(text, "wb") as dst:
            shutil.copyfileobj(src, dst)
        out = os.path.join(tmp, "tsdb")
        r = subprocess.run([_binary("promtool"), "tsdb", "create-blocks-from", "openmetrics", text, out],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"promtool failed: {r.stderr.strip()[-500:]}")
        shutil.move(out, tsdb)
    return tsdb


class Replay:
    def __init__(self, incident_dir: str):
        self.dir = os.path.abspath(incident_dir)
        with open(os.path.join(self.dir, "incident.json")) as f:
            self.incident = json.load(f)
        self.proc: subprocess.Popen | None = None
        self._tmp: tempfile.TemporaryDirectory | None = None
        self._log = None

    def __enter__(self) -> dict:
        tsdb = build_tsdb(self.dir)
        self._tmp = tempfile.TemporaryDirectory()
        config = os.path.join(self._tmp.name, "prometheus.yml")
        with open(config, "w") as f:
            f.write("global: {}\n")  # no scrape jobs, no rules: the recorded data stays as recorded
        port = _free_port()
        self._log = open(os.path.join(self._tmp.name, "prometheus.log"), "w")
        self.proc = subprocess.Popen(
            [_binary("prometheus"), f"--config.file={config}", f"--storage.tsdb.path={tsdb}",
             "--storage.tsdb.retention.time=100y", f"--web.listen-address=127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL, stderr=self._log)
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 30
        while True:
            try:
                urllib.request.urlopen(f"{url}/-/ready", timeout=1)
                break
            except OSError:
                if self.proc.poll() is not None or time.monotonic() > deadline:
                    log = open(os.path.join(self._tmp.name, "prometheus.log")).read()[-500:]
                    self.__exit__()
                    raise RuntimeError(f"replay Prometheus did not start: {log}")
                time.sleep(0.2)
        return {"PROM_URL": url, "PROM_AT": str(self.incident["recorded_at"]),
                "KUBE_FIXTURE": os.path.join(self.dir, "kube")}

    def __exit__(self, *exc) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self._tmp:
            self._log.close()
            self._tmp.cleanup()
            self._tmp = None
