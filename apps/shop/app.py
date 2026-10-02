"""Demo "shop" service used as the incident target.

One image, three roles picked by SERVICE_NAME:
  gateway   -> GET /api/checkout  calls orders
  orders    -> GET /api/orders    calls inventory
  inventory -> GET /api/stock     leaf service

Standard library only (tiny image, no dependency drift).
Exposes Prometheus text metrics on /metrics and JSON logs on stdout.

Fault knobs are ordinary-looking config so the agent cannot read the answer
from a deployment spec:
  RESPONSE_CACHE_ENABLED=true  -> orders caches responses per request id and
                                  never evicts (unbounded growth -> OOMKilled)
  UPSTREAM_URL                 -> wrong host/port breaks the call chain
  REQUEST_VALIDATION_ROUNDS    -> hashing work per request; a large value
                                  saturates the CPU limit and latency climbs
  SERVICE_NAME typo            -> process exits at start (crash loop)
"""

import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROUTES = {"gateway": "/api/checkout", "orders": "/api/orders", "inventory": "/api/stock"}
UPSTREAM_PATH = {"gateway": "/api/orders", "orders": "/api/stock"}
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


class Config:
    def __init__(self, environ=None):
        e = os.environ if environ is None else environ
        self.service = e.get("SERVICE_NAME", "inventory")
        if self.service not in ROUTES:
            raise ValueError(f"unknown SERVICE_NAME {self.service!r}")
        self.port = int(e.get("PORT", "8080"))
        self.upstream_url = e.get("UPSTREAM_URL", "").rstrip("/")
        self.upstream_timeout = float(e.get("UPSTREAM_TIMEOUT_SECONDS", "2"))
        self.cache_enabled = e.get("RESPONSE_CACHE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")
        self.cache_entry_kb = int(e.get("RESPONSE_CACHE_ENTRY_KB", "256"))
        self.validation_rounds = int(e.get("REQUEST_VALIDATION_ROUNDS", "0"))


class Metrics:
    """Minimal thread-safe counters/histogram rendered in Prometheus text format."""

    def __init__(self, service):
        self.service = service
        self.lock = threading.Lock()
        self.requests = {}      # (route, code) -> count
        self.upstream = {}      # (upstream, outcome) -> count
        self.hist = {}          # route -> [bucket counts..., sum, count]
        self.cache_entries = 0

    def observe_request(self, route, code, seconds):
        with self.lock:
            key = (route, str(code))
            self.requests[key] = self.requests.get(key, 0) + 1
            h = self.hist.setdefault(route, [0] * len(BUCKETS) + [0.0, 0])
            for i, b in enumerate(BUCKETS):
                if seconds <= b:
                    h[i] += 1
            h[-2] += seconds
            h[-1] += 1

    def observe_upstream(self, upstream, outcome):
        with self.lock:
            key = (upstream, outcome)
            self.upstream[key] = self.upstream.get(key, 0) + 1

    def render(self):
        s = self.service
        out = [
            "# HELP http_requests_total HTTP requests handled.",
            "# TYPE http_requests_total counter",
        ]
        with self.lock:
            for (route, code), v in sorted(self.requests.items()):
                out.append(f'http_requests_total{{service="{s}",route="{route}",code="{code}"}} {v}')
            out += [
                "# HELP http_request_duration_seconds Request latency.",
                "# TYPE http_request_duration_seconds histogram",
            ]
            for route, h in sorted(self.hist.items()):
                for i, b in enumerate(BUCKETS):
                    out.append(f'http_request_duration_seconds_bucket{{service="{s}",route="{route}",le="{b}"}} {h[i]}')
                out.append(f'http_request_duration_seconds_bucket{{service="{s}",route="{route}",le="+Inf"}} {h[-1]}')
                out.append(f'http_request_duration_seconds_sum{{service="{s}",route="{route}"}} {h[-2]:.6f}')
                out.append(f'http_request_duration_seconds_count{{service="{s}",route="{route}"}} {h[-1]}')
            out += [
                "# HELP upstream_requests_total Calls made to the upstream service.",
                "# TYPE upstream_requests_total counter",
            ]
            for (up, outcome), v in sorted(self.upstream.items()):
                out.append(f'upstream_requests_total{{service="{s}",upstream="{up}",outcome="{outcome}"}} {v}')
            out += [
                "# HELP response_cache_entries Entries held in the response cache.",
                "# TYPE response_cache_entries gauge",
                f'response_cache_entries{{service="{s}"}} {self.cache_entries}',
            ]
        return "\n".join(out) + "\n"


def log(service, level, msg, **fields):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()), "level": level,
           "service": service, "msg": msg}
    rec.update(fields)
    sys.stdout.write(json.dumps(rec) + "\n")
    sys.stdout.flush()


class App:
    def __init__(self, cfg):
        self.cfg = cfg
        self.metrics = Metrics(cfg.service)
        self._cache = {}
        self._cache_lock = threading.Lock()

    def upstream_name(self):
        return {"gateway": "orders", "orders": "inventory"}.get(self.cfg.service)

    def call_upstream(self, request_id):
        """Returns (status, body_dict). Maps upstream failures to 502/503/504."""
        up = self.upstream_name()
        url = f"{self.cfg.upstream_url}{UPSTREAM_PATH[self.cfg.service]}"
        req = urllib.request.Request(url, headers={"X-Request-Id": request_id})
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.upstream_timeout) as r:
                body = json.loads(r.read() or b"{}")
            self.metrics.observe_upstream(up, "ok")
            return 200, body
        except urllib.error.HTTPError as e:
            self.metrics.observe_upstream(up, f"http_{e.code}")
            log(self.cfg.service, "error", "upstream returned error status",
                upstream=up, url=url, status=e.code, request_id=request_id)
            return 502, {"error": f"{up} returned {e.code}"}
        except (urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            text = str(reason)
            if isinstance(reason, socket.gaierror):  # glibc and musl word this differently
                outcome, code = "dns_error", 503
            elif "timed out" in text.lower():
                outcome, code = "timeout", 504
            elif "refused" in text.lower():
                outcome, code = "connection_refused", 503
            else:
                outcome, code = "error", 503
            self.metrics.observe_upstream(up, outcome)
            log(self.cfg.service, "error", "upstream call failed",
                upstream=up, url=url, error=text, request_id=request_id)
            return code, {"error": f"{up} unavailable"}

    def validate_request(self, request_id):
        """CPU-bound check on every request; cost grows linearly with the rounds."""
        digest = request_id.encode()
        for _ in range(self.cfg.validation_rounds):
            digest = hashlib.sha256(digest).digest()
        return digest

    def handle_business(self, request_id):
        svc = self.cfg.service
        self.validate_request(request_id)
        if svc == "inventory":
            return 200, {"sku": "SKU-1042", "available": 17}
        status, body = self.call_upstream(request_id)
        if status != 200:
            return status, body
        if svc == "orders":
            result = {"order_id": request_id[:8], "stock": body}
            if self.cfg.cache_enabled:
                # Cache keyed by request id with no eviction: every request adds an entry.
                with self._cache_lock:
                    # Filled (not zeroed) bytes so pages are really touched and RSS grows.
                    self._cache[request_id] = (result, b"\x01" * (self.cfg.cache_entry_kb * 1024))
                    self.metrics.cache_entries = len(self._cache)
            return 200, result
        return 200, {"checkout": "ok", "order": body}


def make_handler(app):
    route = ROUTES[app.cfg.service]

    class Handler(BaseHTTPRequestHandler):
        server_version = "shop/1.0"

        def log_message(self, *args):  # silence default access log; we log JSON
            pass

        def _send(self, code, payload, ctype="application/json"):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/healthz", "/readyz"):
                return self._send(200, {"status": "ok"})
            if path == "/metrics":
                return self._send(200, app.metrics.render().encode(), "text/plain; version=0.0.4")
            if path != route:
                return self._send(404, {"error": "not found"})
            rid = self.headers.get("X-Request-Id") or str(uuid.uuid4())
            start = time.monotonic()
            try:
                code, body = app.handle_business(rid)
            except Exception as e:  # pragma: no cover - defensive
                log(app.cfg.service, "error", "unhandled exception", error=repr(e), request_id=rid)
                code, body = 500, {"error": "internal"}
            app.metrics.observe_request(route, code, time.monotonic() - start)
            self._send(code, body)

    return Handler


def serve(cfg):
    app = App(cfg)
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(app))
    log(cfg.service, "info", "starting", port=cfg.port, upstream=cfg.upstream_url or None,
        response_cache=cfg.cache_enabled)
    return app, httpd


def main():
    try:
        cfg = Config()
    except ValueError as e:
        log(os.environ.get("SERVICE_NAME", "?"), "fatal", "invalid configuration", error=str(e))
        sys.exit(1)
    if cfg.service != "inventory" and not cfg.upstream_url:
        log(cfg.service, "fatal", "invalid configuration", error="UPSTREAM_URL is required")
        sys.exit(1)
    _, httpd = serve(cfg)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
