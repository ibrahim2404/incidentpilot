import json
import threading
import unittest
import urllib.error
import urllib.request

from app import Config, serve


def start(env):
    env = {"PORT": "0", **env}
    app, httpd = serve(Config(env))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return app, httpd, f"http://127.0.0.1:{httpd.server_address[1]}"


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


class ChainTest(unittest.TestCase):
    def setUp(self):
        self.servers = []

    def tearDown(self):
        for h in self.servers:
            h.shutdown()
            h.server_close()

    def chain(self, orders_env=None, inventory_up=True):
        inv_url = "http://127.0.0.1:1"  # closed port -> connection refused
        if inventory_up:
            _, h, inv_url = start({"SERVICE_NAME": "inventory"})
            self.servers.append(h)
        orders, h, ord_url = start({"SERVICE_NAME": "orders", "UPSTREAM_URL": inv_url, **(orders_env or {})})
        self.servers.append(h)
        gw, h, gw_url = start({"SERVICE_NAME": "gateway", "UPSTREAM_URL": ord_url})
        self.servers.append(h)
        return gw_url, ord_url, orders

    def test_happy_path_and_metrics(self):
        gw, _, _ = self.chain()
        code, body = get(gw + "/api/checkout")
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["checkout"], "ok")
        _, metrics = get(gw + "/metrics")
        self.assertIn('http_requests_total{service="gateway",route="/api/checkout",code="200"} 1', metrics)
        self.assertIn('upstream_requests_total{service="gateway",upstream="orders",outcome="ok"} 1', metrics)

    def test_dependency_down_propagates(self):
        gw, ord_url, _ = self.chain(inventory_up=False)
        code, _ = get(gw + "/api/checkout")
        self.assertEqual(code, 502)  # gateway sees orders' 503
        _, m = get(ord_url + "/metrics")
        self.assertIn('outcome="connection_refused"', m)
        self.assertIn('code="503"', m)

    def test_cache_grows_without_eviction(self):
        gw, _, orders = self.chain(orders_env={"RESPONSE_CACHE_ENABLED": "true", "RESPONSE_CACHE_ENTRY_KB": "1"})
        for _ in range(5):
            get(gw + "/api/checkout")
        self.assertEqual(orders.metrics.cache_entries, 5)

    def test_bad_upstream_host_is_dns_error(self):
        _, h, url = start({"SERVICE_NAME": "gateway", "UPSTREAM_URL": "http://orders.invalid:8080"})
        self.servers.append(h)
        code, _ = get(url + "/api/checkout")
        self.assertEqual(code, 503)
        _, m = get(url + "/metrics")
        self.assertIn('outcome="dns_error"', m)

    def test_unknown_service_rejected(self):
        with self.assertRaises(ValueError):
            Config({"SERVICE_NAME": "payments"})


if __name__ == "__main__":
    unittest.main()
