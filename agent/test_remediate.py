"""Guardrail tests of the remediation loop. A fake kubectl serves cluster state and records every
command, so each test can assert exactly what would have changed in the cluster (often: nothing)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import remediate as r  # noqa: E402


def deployment(name, replicas=1, revision=3, image="incidentpilot/shop:dev", memory="96Mi", probe="/healthz"):
    return {"metadata": {"name": name, "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
            "spec": {"replicas": replicas, "template": {"metadata": {"labels": {"app": name}}, "spec": {"containers": [{
                "name": name, "image": image,
                "resources": {"requests": {"memory": "32Mi"}, "limits": {"memory": memory}},
                "livenessProbe": {"httpGet": {"path": probe}}}]}}}}


def replicaset(owner, revision, **kw):
    d = deployment(owner, **kw)
    return {"metadata": {"name": f"{owner}-{revision}", "ownerReferences": [{"name": owner}],
                         "annotations": {"deployment.kubernetes.io/revision": str(revision)}},
            "spec": {"template": d["spec"]["template"]}}


class FakeKubectl:
    def __init__(self, objects=None, fail_on=None, switch="enabled"):
        self.objects = {("configmap", r.CONTROL_CONFIGMAP): {"data": {"remediation": switch}} if switch else None}
        self.objects.update(objects or {})
        self.fail_on = fail_on or ()
        self.calls = []

    def json(self, *args):
        verb, kind = args[0], args[1]
        if verb == "get" and kind in ("replicasets", "networkpolicies"):
            return {"items": self.objects.get((kind,), [])}
        obj = self.objects.get((kind, args[2]))
        if obj is None:
            raise r.Refused(f"{kind} {args[2]} not found")
        return obj

    def run(self, *args, stdin=None):
        self.calls.append(list(args))
        if any(f in args for f in self.fail_on) and "--dry-run=server" not in args:
            raise r.Refused(f"simulated failure on {args[0]}")
        return ""

    def writes(self):
        return [c for c in self.calls if "--dry-run=server" not in c]


def diag(component="inventory", fault="dependency_down", action="scale_up"):
    return {"component": component, "fault": fault, "proposed_action": action, "confidence": 0.9, "evidence": ["x"]}


def cleared(*_):
    return "cleared", 42.0


def still_firing(*_):
    return "still_firing", 480.0


class RemediateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        r.AUDIT = os.path.join(self.tmp.name, "audit.jsonl")
        r.STOP_FILE = os.path.join(self.tmp.name, "stop")

    def tearDown(self):
        self.tmp.cleanup()

    def go(self, k, d, approve=True, verifier=cleared):
        return r.remediate(k, d, {"HighErrorRate"}, "http://prom", lambda *_: approve, verifier=verifier)

    # --- the happy path ---
    def test_scale_up_dry_run_then_apply_then_verified(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})
        rec = self.go(k, diag())
        self.assertEqual(rec["outcome"], "mitigated")
        self.assertEqual(k.calls, [["scale", "deployment/inventory", "--replicas=1", "--dry-run=server"],
                                   ["scale", "deployment/inventory", "--replicas=1"]])
        with open(r.AUDIT) as f:
            self.assertEqual(json.loads(f.readline())["outcome"], "mitigated")

    # --- guardrails: nothing is written ---
    def test_kill_switch_off_or_missing_blocks_everything(self):
        for switch in ("disabled", None):
            k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)}, switch=switch)
            self.assertEqual(self.go(k, diag())["outcome"], "blocked_by_kill_switch")
            self.assertEqual(k.calls, [])

    def test_local_stop_file_blocks(self):
        open(r.STOP_FILE, "w").close()
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})
        self.assertEqual(self.go(k, diag())["outcome"], "blocked_by_kill_switch")

    def test_action_outside_allowlist_refused(self):
        k = FakeKubectl()
        for action in ("delete_namespace", "exec", "kubectl delete deployment orders"):
            rec = self.go(k, diag(action=action))
            self.assertEqual(rec["outcome"], "refused")
            self.assertIn("allowlist", rec["reason"])
        self.assertEqual(k.calls, [])

    def test_component_outside_managed_services_refused(self):
        k = FakeKubectl()
        for component in ("kube-system", "loadgen", "orders; rm -rf /", "unknown"):
            self.assertEqual(self.go(k, diag(component=component))["outcome"], "refused")
        self.assertEqual(k.calls, [])

    def test_model_free_text_target_is_ignored(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})
        d = dict(diag(), action_target="kube-system/coredns")
        self.go(k, d)
        self.assertTrue(all("deployment/inventory" in c for c in k.calls))

    def test_human_says_no(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})
        rec = self.go(k, diag(), approve=False)
        self.assertEqual(rec["outcome"], "rejected_by_human")
        self.assertEqual(k.writes(), [])  # only the dry run reached the API server

    def test_kill_switch_flipped_during_approval(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})

        def approve_then_flip(*_):
            k.objects[("configmap", r.CONTROL_CONFIGMAP)] = {"data": {"remediation": "disabled"}}
            return True
        rec = r.remediate(k, diag(), {"HighErrorRate"}, "p", approve_then_flip, verifier=cleared)
        self.assertEqual(rec["outcome"], "blocked_by_kill_switch")
        self.assertEqual(k.writes(), [])

    def test_dry_run_rejection_stops_before_approval(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})
        k.run = lambda *a, **kw: (_ for _ in ()).throw(r.Refused("forbidden: cannot patch"))
        asked = []
        rec = r.remediate(k, diag(), {"X"}, "p", lambda *_: asked.append(1) or True, verifier=cleared)
        self.assertEqual(rec["outcome"], "refused")
        self.assertIn("dry run rejected", rec["reason"])
        self.assertEqual(asked, [])  # a human is never asked to approve something that cannot run

    # --- verification and rollback ---
    def test_not_cleared_rolls_back(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})
        rec = self.go(k, diag(), verifier=still_firing)
        self.assertEqual(rec["outcome"], "not_mitigated_rolled_back")
        self.assertEqual(k.writes()[-1], ["scale", "deployment/inventory", "--replicas=0"])

    def test_unverifiable_does_not_roll_back(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=0)})
        rec = self.go(k, diag(), verifier=lambda *_: ("unverifiable", 45.0))
        self.assertEqual(rec["outcome"], "applied_unverified")
        self.assertEqual(len(k.writes()), 1)

    def test_apply_failure_runs_undo(self):
        k = FakeKubectl({("deployment", "inventory"): deployment("inventory", replicas=1)}, fail_on=("rollout",))
        rec = self.go(k, diag(action="restart_deployment"))
        self.assertEqual(rec["outcome"], "apply_failed_rolled_back")

    # --- plans ---
    def test_rollback_config_uses_git_manifest_as_truth(self):
        live = {"data": {"SERVICE_NAME": "inventroy", "REQUEST_VALIDATION_ROUNDS": "1000"}}
        k = FakeKubectl({("configmap", "inventory-config"): live})
        plan = r.build_plan(k, diag(fault="crash_loop", action="rollback_config"))
        self.assertEqual(plan.diff, ["configmap inventory-config: SERVICE_NAME: 'inventroy' -> 'inventory'"])
        self.assertIn('{"data": {"SERVICE_NAME": "inventory"}}', plan.steps[0].args)
        self.assertIn('{"data": {"SERVICE_NAME": "inventroy"}}', plan.undo[0].args)

    def test_rollback_config_refused_when_nothing_changed(self):
        k = FakeKubectl({("configmap", "inventory-config"): {"data": {"SERVICE_NAME": "inventory",
                                                                       "REQUEST_VALIDATION_ROUNDS": "1000"}}})
        with self.assertRaises(r.Refused):
            r.build_plan(k, diag(action="rollback_config"))

    def test_rollback_deployment_diff_and_restart_only_revision_refused(self):
        k = FakeKubectl({("deployment", "orders"): deployment("orders", revision=3, image="incidentpilot/shop:2.0"),
                         ("replicasets",): [replicaset("orders", 2), replicaset("orders", 3, image="incidentpilot/shop:2.0")]})
        plan = r.build_plan(k, diag("orders", "bad_image", "rollback_deployment"))
        self.assertEqual(plan.steps[0].args, ["rollout", "undo", "deployment/orders", "--to-revision=2"])
        self.assertTrue(any("shop:2.0 -> incidentpilot/shop:dev" in line for line in plan.diff))
        same = FakeKubectl({("deployment", "orders"): deployment("orders", revision=3),
                            ("replicasets",): [replicaset("orders", 2)]})
        with self.assertRaises(r.Refused):
            r.build_plan(same, diag("orders", "bad_image", "rollback_deployment"))

    def test_scale_cap(self):
        k = FakeKubectl({("deployment", "gateway"): deployment("gateway", replicas=3)})
        with self.assertRaises(r.Refused):
            r.build_plan(k, diag("gateway", "traffic_surge", "scale_up"))

    def test_raise_memory_restores_manifest_then_doubles_with_cap(self):
        low = FakeKubectl({("deployment", "orders"): deployment("orders", memory="10Mi")})
        self.assertEqual(r.build_plan(low, diag("orders", "resource_limit", "raise_memory_limit")).diff,
                         ["deployment orders memory limit: 10Mi -> 96Mi"])
        at_cap = FakeKubectl({("deployment", "orders"): deployment("orders", memory="256Mi")})
        with self.assertRaises(r.Refused):
            r.build_plan(at_cap, diag("orders", "memory_leak", "raise_memory_limit"))

    def test_remove_network_policy_only_those_selecting_the_component(self):
        pols = [{"metadata": {"name": "inventory-ingress", "uid": "u1", "resourceVersion": "9"},
                 "spec": {"podSelector": {"matchLabels": {"app": "inventory"}}}},
                {"metadata": {"name": "orders-only"}, "spec": {"podSelector": {"matchLabels": {"app": "orders"}}}}]
        k = FakeKubectl({("networkpolicies",): pols})
        plan = r.build_plan(k, diag("inventory", "network_block", "remove_network_policy"))
        self.assertEqual([s.args for s in plan.steps], [["delete", "networkpolicy", "inventory-ingress"]])
        restored = json.loads(plan.undo[0].stdin)
        self.assertNotIn("uid", restored["metadata"])  # re-creatable object, not a stale copy

    def test_quantities(self):
        self.assertEqual(r.mebibytes("96Mi"), 96)
        self.assertEqual(r.mebibytes("1Gi"), 1024)
        self.assertAlmostEqual(r.mebibytes("134217728"), 128)


if __name__ == "__main__":
    unittest.main()
