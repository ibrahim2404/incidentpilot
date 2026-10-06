"""Manual check of the backend against a live Prometheus (needs the port-forward on 9090)."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from backend import LiveBackend, PrometheusError  # noqa: E402

prom = LiveBackend(os.environ.get("PROM_URL", "http://localhost:9090"))

print("1. instant 'up':")
for series in prom.instant("up"):
    print("  ", series["metric"].get("job"), series["metric"].get("pod", ""), "->", series["value"][1])

print("2. range, orders memory over 10 min:")
end = prom.now()
for series in prom.range('container_memory_working_set_bytes{container="orders"} / 1024 / 1024', end - 600, end, 60):
    print("  ", [round(float(v)) for _, v in series["values"]])

print("3. a broken query:")
try:
    prom.instant("sum(rate(http_requests_total[1m]")
except PrometheusError as e:
    print("   PrometheusError:", e)

print("4. alerts firing now:")
try:
    for a in prom.alerts():
        print("  ", a["metric"]["alertname"], a["metric"].get("service", ""))
    print("   (end of list)")
except PrometheusError as e:
    print("   PrometheusError:", e)