"""Closed-loop remediation with a human in the loop.

  python agent/remediate.py --incident runs/S002-<ts>.json --result results/S002-<ts>.json

The model chooses WHAT (one action from the allowlist); this code decides WHERE and HOW:
  1. kill switch   configmap shop/incidentpilot-control must say remediation=enabled (missing = off),
                   and no local .incidentpilot-stop file
  2. plan          target derived from the diagnosed component (the model's free-text target is
                   ignored), current state re-read from the cluster, diff computed against it
  3. dry run       every command is sent with --dry-run=server: the API server validates it and the
                   writer's RBAC is checked, nothing changes
  4. approval      a human reads the diagnosis, evidence and diff and types "yes"
  5. apply         with .kube/remediator.yaml, a ServiceAccount allowed only these verbs on these objects
  6. verify        the incident's alerts must stop firing within --verify-timeout
  7. rollback      if they do not, the recorded undo steps restore the previous state
Every decision is appended to results/audit.jsonl.
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from taxonomy import ACTIONS, COMPONENTS  # noqa: E402

NAMESPACE = "shop"
CONTROL_CONFIGMAP = "incidentpilot-control"
STOP_FILE = os.path.join(ROOT, ".incidentpilot-stop")
MANIFEST = os.path.join(ROOT, "deploy", "shop", "shop.yaml")
AUDIT = os.path.join(ROOT, "results", "audit.jsonl")
MAX_REPLICAS = 3
MAX_MEMORY_MI = 256
NAME = re.compile(r"^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$")


class Refused(Exception):
    """The remediation must not run. The message says why (shown to the human, written to the audit)."""


class Kubectl:
    """kubectl in namespace shop as the remediator identity."""

    def __init__(self, kubeconfig: str | None, timeout_s: float = 60):
        self.kubeconfig, self.timeout_s = kubeconfig, timeout_s

    def run(self, *args: str, stdin: str | None = None) -> str:
        cmd = ["kubectl", *(["--kubeconfig", self.kubeconfig] if self.kubeconfig else []), "-n", NAMESPACE, *args]
        r = subprocess.run(cmd, input=stdin, capture_output=True, text=True, timeout=self.timeout_s)
        if r.returncode != 0:
            raise Refused(f"kubectl {' '.join(args[:3])} failed: {r.stderr.strip()[:300]}")
        return r.stdout

    def json(self, *args: str) -> dict:
        return json.loads(self.run(*args, "-o", "json"))


@dataclass
class Step:
    args: list[str]
    stdin: str | None = None

    def show(self) -> str:
        return "kubectl -n shop " + " ".join(a if " " not in a and "{" not in a else repr(a) for a in self.args)


@dataclass
class Plan:
    action: str
    target: str
    diff: list[str]
    steps: list[Step] = field(default_factory=list)
    undo: list[Step] = field(default_factory=list)


# ---------- helpers ----------

def mebibytes(q: str) -> float:
    """'96Mi' -> 96, '1Gi' -> 1024, '100M' -> 95.4, '134217728' -> 128."""
    m = re.fullmatch(r"([0-9.]+)([KMGT]i?)?", str(q))
    if not m:
        raise Refused(f"cannot parse memory quantity {q!r}")
    factor = {None: 1 / 2**20, "Ki": 1 / 1024, "Mi": 1, "Gi": 1024, "Ti": 2**20,
              "K": 1e3 / 2**20, "M": 1e6 / 2**20, "G": 1e9 / 2**20, "T": 1e12 / 2**20}[m.group(2)]
    return float(m.group(1)) * factor


def manifest_objects(kind: str) -> dict[str, dict]:
    """The desired state kept in git (deploy/shop/shop.yaml), by name."""
    import yaml
    with open(MANIFEST) as f:
        return {d["metadata"]["name"]: d for d in yaml.safe_load_all(f) if d and d.get("kind") == kind}


def selects(policy: dict, labels: dict) -> bool:
    sel = policy.get("spec", {}).get("podSelector", {})
    if sel.get("matchExpressions"):
        return True
    return all(labels.get(k) == v for k, v in sel.get("matchLabels", {}).items())


def container_view(template: dict) -> dict:
    c = template["spec"]["containers"][0]
    return {"image": c.get("image"), "resources": c.get("resources", {}),
            "livenessProbe": c.get("livenessProbe"), "readinessProbe": c.get("readinessProbe")}


# ---------- plans ----------

def plan_rollback_config(k: Kubectl, svc: str) -> Plan:
    name = f"{svc}-config"
    live = k.json("get", "configmap", name).get("data", {})
    desired = manifest_objects("ConfigMap")[name].get("data", {})
    changes = {key: desired.get(key) for key in sorted(set(live) | set(desired)) if live.get(key) != desired.get(key)}
    if not changes:
        raise Refused(f"{name} already matches the manifest in git: rollback_config would change nothing")
    diff = [f"configmap {name}: {key}: {live.get(key)!r} -> " + ("(removed)" if new is None else repr(new))
            for key, new in changes.items()]
    restore = {key: live.get(key) for key in changes}
    return Plan("rollback_config", name, diff,
                steps=[Step(["patch", "configmap", name, "--type", "merge", "-p", json.dumps({"data": changes})]),
                       Step(["rollout", "restart", f"deployment/{svc}"])],
                undo=[Step(["patch", "configmap", name, "--type", "merge", "-p", json.dumps({"data": restore})]),
                      Step(["rollout", "restart", f"deployment/{svc}"])])


def plan_rollback_deployment(k: Kubectl, svc: str) -> Plan:
    d = k.json("get", "deployment", svc)
    current = int(d["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision", "0"))
    owned = [rs for rs in k.json("get", "replicasets")["items"]
             if any(o.get("name") == svc for o in rs["metadata"].get("ownerReferences", []))]
    older = sorted((int(rs["metadata"]["annotations"].get("deployment.kubernetes.io/revision", "0")), rs)
                   for rs in owned)
    older = [(rev, rs) for rev, rs in older if rev < current]
    if not older:
        raise Refused(f"deployment {svc} has no previous revision to roll back to")
    prev_rev, prev = older[-1]
    now_view, prev_view = container_view(d["spec"]["template"]), container_view(prev["spec"]["template"])
    diff = [f"deployment {svc} {key}: {now_view[key]} -> {prev_view[key]}"
            for key in now_view if now_view[key] != prev_view[key]]
    if not diff:
        raise Refused(f"revision {prev_rev} of {svc} has the same image, limits and probes: "
                      "rolling back would only restart it")
    return Plan("rollback_deployment", svc, [f"revision {current} -> {prev_rev}"] + diff,
                steps=[Step(["rollout", "undo", f"deployment/{svc}", f"--to-revision={prev_rev}"])],
                undo=[Step(["rollout", "undo", f"deployment/{svc}"])])


def plan_scale_up(k: Kubectl, svc: str) -> Plan:
    replicas = int(k.json("get", "deployment", svc)["spec"].get("replicas", 1))
    if replicas >= MAX_REPLICAS:
        raise Refused(f"{svc} already has {replicas} replicas (cap {MAX_REPLICAS})")
    new = 1 if replicas == 0 else replicas + 1
    return Plan("scale_up", svc, [f"deployment {svc} replicas: {replicas} -> {new}"],
                steps=[Step(["scale", f"deployment/{svc}", f"--replicas={new}"])],
                undo=[Step(["scale", f"deployment/{svc}", f"--replicas={replicas}"])])


def plan_restart(k: Kubectl, svc: str) -> Plan:
    k.json("get", "deployment", svc)  # exists and readable
    return Plan("restart_deployment", svc, [f"deployment {svc}: rolling restart, no spec change"],
                steps=[Step(["rollout", "restart", f"deployment/{svc}"])], undo=[])


def plan_raise_memory(k: Kubectl, svc: str) -> Plan:
    c = k.json("get", "deployment", svc)["spec"]["template"]["spec"]["containers"][0]
    res = c.get("resources", {})
    limit = res.get("limits", {}).get("memory")
    if not limit:
        raise Refused(f"{svc} has no memory limit to raise")
    desired = manifest_objects("Deployment")[svc]["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
    cur, want = mebibytes(limit), mebibytes(desired)
    new = want if cur < want else min(cur * 2, MAX_MEMORY_MI)
    if new <= cur:
        raise Refused(f"{svc} memory limit {limit} is already at the cap of {MAX_MEMORY_MI}Mi")
    new_q = f"{int(new)}Mi"
    request = res.get("requests", {}).get("memory")
    args = ["set", "resources", f"deployment/{svc}", "-c", c["name"], f"--limits=memory={new_q}"]
    undo = ["set", "resources", f"deployment/{svc}", "-c", c["name"], f"--limits=memory={limit}"]
    if request:
        undo.append(f"--requests=memory={request}")
    return Plan("raise_memory_limit", svc, [f"deployment {svc} memory limit: {limit} -> {new_q}"],
                steps=[Step(args)], undo=[Step(undo)])


def plan_remove_network_policy(k: Kubectl, svc: str) -> Plan:
    policies = [p for p in k.json("get", "networkpolicies")["items"] if selects(p, {"app": svc})]
    if not policies:
        raise Refused(f"no network policy selects the {svc} pods")
    steps, undo, diff = [], [], []
    for p in policies:
        name = p["metadata"]["name"]
        saved = copy.deepcopy(p)
        for key in ("resourceVersion", "uid", "creationTimestamp", "managedFields", "generation"):
            saved["metadata"].pop(key, None)
        saved.pop("status", None)
        diff.append(f"networkpolicy {name} (selects {svc}): delete")
        steps.append(Step(["delete", "networkpolicy", name]))
        undo.append(Step(["apply", "-f", "-"], stdin=json.dumps(saved)))
    return Plan("remove_network_policy", svc, diff, steps=steps, undo=undo)


PLANNERS: dict[str, Callable[[Kubectl, str], Plan]] = {
    "rollback_config": plan_rollback_config,
    "rollback_deployment": plan_rollback_deployment,
    "scale_up": plan_scale_up,
    "restart_deployment": plan_restart,
    "raise_memory_limit": plan_raise_memory,
    "remove_network_policy": plan_remove_network_policy,
}
assert set(PLANNERS) | {"none"} == set(ACTIONS), "every allowed action needs a planner"


def build_plan(k: Kubectl, diagnosis: dict) -> Plan:
    action, svc = diagnosis.get("proposed_action"), diagnosis.get("component")
    if action not in ACTIONS:
        raise Refused(f"action {action!r} is not in the allowlist {list(ACTIONS)}")
    if action == "none":
        return Plan("none", "-", ["no action proposed"])
    if svc not in COMPONENTS:
        raise Refused(f"component {svc!r} is not one of the managed services {COMPONENTS}")
    return PLANNERS[action](k, svc)


# ---------- safety checks ----------

def kill_switch_reason(k: Kubectl) -> str | None:
    """None when remediation may run; otherwise why not. Fail closed: no switch = off."""
    if os.path.exists(STOP_FILE):
        return f"local stop file {STOP_FILE} exists"
    try:
        data = k.json("get", "configmap", CONTROL_CONFIGMAP).get("data", {})
    except Refused as e:
        return f"cannot read {CONTROL_CONFIGMAP} ({e}); remediation stays off"
    if data.get("remediation") != "enabled":
        return f"{CONTROL_CONFIGMAP} remediation={data.get('remediation')!r} (must be 'enabled')"
    return None


def run_steps(k: Kubectl, steps: list[Step], dry_run: bool) -> None:
    for s in steps:
        k.run(*s.args, *(["--dry-run=server"] if dry_run else []), stdin=s.stdin)


def firing(prom_url: str, names: set[str]) -> set[str] | None:
    """Names among `names` firing now in namespace shop; None if Prometheus cannot be reached."""
    import httpx
    try:
        r = httpx.get(f"{prom_url}/api/v1/query", params={"query": 'ALERTS{alertstate="firing"}'}, timeout=10)
        result = r.json()["data"]["result"]
    except Exception:
        return None
    return {s["metric"]["alertname"] for s in result} & names


def verify(prom_url: str, names: set[str], timeout_s: float, poll_s: float = 15,
           clock=time.monotonic, sleep=time.sleep) -> tuple[str, float]:
    """('cleared'|'still_firing'|'unverifiable', seconds). Alerts with a 'for' or a [5m] window
    take minutes to clear after the fix, hence a generous timeout."""
    start = clock()
    unreachable = 0
    while True:
        now = firing(prom_url, names)
        if now is None:
            unreachable += 1
            if unreachable >= 3:
                return "unverifiable", clock() - start
        elif not now:
            return "cleared", clock() - start
        if clock() - start >= timeout_s:
            return "still_firing", clock() - start
        sleep(poll_s)


def audit(entry: dict) -> None:
    os.makedirs(os.path.dirname(AUDIT), exist_ok=True)
    entry = {"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **entry}
    with open(AUDIT, "a") as f:
        f.write(json.dumps(entry) + "\n")


def ask_human(diagnosis: dict, plan: Plan) -> bool:
    print("\n=== Proposed remediation (nothing has changed yet; dry run passed) ===")
    print(f"diagnosis: {diagnosis.get('component')} / {diagnosis.get('fault')} "
          f"(confidence {diagnosis.get('confidence')})")
    for e in diagnosis.get("evidence", []):
        print(f"  evidence: {e}")
    print(f"action: {plan.action} on {plan.target}")
    for line in plan.diff:
        print(f"  change: {line}")
    for s in plan.steps:
        print(f"  will run: {s.show()}")
    print(f"  if the alerts do not clear: {'; '.join(s.show() for s in plan.undo) or 'nothing to undo'}")
    return input('Type "yes" to apply, anything else to refuse: ').strip() == "yes"


# ---------- the loop ----------

def remediate(k: Kubectl, diagnosis: dict, alert_names: set[str], prom_url: str,
              approve: Callable[[dict, Plan], bool], verify_timeout_s: float = 480,
              verifier=verify, clock=time.monotonic, approval_mode: str = "interactive",
              detected_at: dt.datetime | None = None, scenario: str | None = None) -> dict:
    rec: dict = {"scenario": scenario, "approval": approval_mode,
                 "diagnosis": {k_: diagnosis.get(k_) for k_ in ("component", "fault", "proposed_action")},
                 "alerts": sorted(alert_names)}
    t0 = clock()

    def done(outcome: str, **extra) -> dict:
        rec.update(outcome=outcome, total_s=round(clock() - t0, 1), **extra)
        if outcome == "mitigated" and detected_at:  # alert fired -> alerts verified clear
            rec["time_to_mitigate_s"] = round((dt.datetime.now(dt.timezone.utc) - detected_at).total_seconds())
        audit(rec)
        return rec

    if reason := kill_switch_reason(k):
        return done("blocked_by_kill_switch", reason=reason)
    try:
        plan = build_plan(k, diagnosis)
    except Refused as e:
        return done("refused", reason=str(e))
    rec["plan"] = {"action": plan.action, "target": plan.target, "diff": plan.diff,
                   "steps": [s.show() for s in plan.steps], "undo": [s.show() for s in plan.undo]}
    if plan.action == "none":
        return done("no_action")
    try:
        run_steps(k, plan.steps, dry_run=True)
    except Refused as e:
        return done("refused", reason=f"dry run rejected: {e}")

    t_ask = clock()
    approved = approve(diagnosis, plan)
    rec["approval_wait_s"] = round(clock() - t_ask, 1)
    if not approved:
        return done("rejected_by_human")
    if reason := kill_switch_reason(k):  # it may have been flipped while the human was reading
        return done("blocked_by_kill_switch", reason=reason)

    t_apply = clock()
    try:
        run_steps(k, plan.steps, dry_run=False)
    except Refused as e:
        try:
            run_steps(k, plan.undo, dry_run=False)
            return done("apply_failed_rolled_back", reason=str(e))
        except Refused as e2:
            return done("apply_failed_rollback_failed", reason=f"{e}; rollback: {e2}")

    status, took = verifier(prom_url, alert_names, verify_timeout_s)
    rec.update(verification=status, verify_s=round(took, 1), apply_to_verified_s=round(clock() - t_apply, 1))
    if status == "cleared":
        return done("mitigated")
    if status == "unverifiable":
        return done("applied_unverified", reason="Prometheus unreachable: a human must check; no automatic rollback")
    if reason := kill_switch_reason(k):
        return done("not_mitigated_kill_switch", reason=f"rollback skipped: {reason}")
    try:
        run_steps(k, plan.undo, dry_run=False)
        return done("not_mitigated_rolled_back")
    except Refused as e:
        return done("not_mitigated_rollback_failed", reason=str(e))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--incident", required=True, help="runs/<id>-<ts>.json (alerts and detection time)")
    p.add_argument("--result", required=True, help="results/<id>-<ts>.json written by agent/run.py")
    p.add_argument("--verify-timeout", type=float, default=480)
    p.add_argument("--approve", action="store_true",
                   help="pre-approve (unattended experiments only; recorded as such in the audit)")
    a = p.parse_args()
    sys.path.insert(0, HERE)
    from run import load_env
    load_env(os.path.join(ROOT, ".env"))

    with open(a.incident) as f:
        incident = json.load(f)
    with open(a.result) as f:
        result = json.load(f)
    if not result.get("diagnosis"):
        raise SystemExit(f"no diagnosis in {a.result} ({result.get('stop_reason')}): nothing to remediate")
    names = {al["labels"]["alertname"] for al in incident["alerts"]}
    k = Kubectl(os.environ.get("REMEDIATOR_KUBECONFIG", os.path.join(ROOT, ".kube", "remediator.yaml")))
    approve = (lambda d, plan: True) if a.approve else ask_human
    detected = dt.datetime.fromisoformat(incident["started_at"]) + dt.timedelta(seconds=incident["seconds_to_alert"])

    rec = remediate(k, result["diagnosis"], names, os.environ.get("PROM_URL", "http://localhost:9090"),
                    approve, a.verify_timeout, approval_mode="pre-approved flag" if a.approve else "interactive",
                    detected_at=detected, scenario=incident.get("scenario"))
    result["remediation"] = rec
    with open(a.result, "w") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(rec, indent=2))


if __name__ == "__main__":
    main()
