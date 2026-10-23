"""Single source of truth for what a diagnosis can say. Used by the scenario runner (validation),
the agent (submit_diagnosis enums and the prompt) and the scorer, so they cannot drift apart."""

COMPONENTS = ["gateway", "orders", "inventory"]

# fault -> definition shown to the agent. Definitions describe mechanisms, never scenarios.
FAULTS = {
    "memory_leak": "memory grows over time until the container is OOMKilled, under an unchanged memory limit",
    "resource_limit": "a CPU or memory limit was lowered in the deployment spec, so a normal workload no "
                      "longer fits (OOMKilled at start, or CPU throttled)",
    "crash_loop": "the process exits right after starting (bad startup config), CrashLoopBackOff",
    "bad_config": "a wrong setting in a configmap breaks behaviour while the process keeps running",
    "bad_image": "the deployment points at an image that cannot be pulled or run (ErrImagePull, ImagePullBackOff)",
    "probe_misconfig": "a liveness or readiness probe is wrong, so Kubernetes kills or unreadies a healthy process",
    "dependency_down": "the component has no running or ready replicas, so its callers fail "
                       "(name the unavailable component)",
    "network_block": "the component runs and is ready, but network traffic to it is blocked (NetworkPolicy)",
    "cpu_saturation": "the component's own work per request grew (config) and CPU sits at the limit, latency up",
    "traffic_surge": "incoming request rate rose far above normal with no change to the services",
    "none": "no real problem",
}

# remediation the agent proposes to a human; the guardrail layer only allows these.
ACTIONS = {
    "rollback_config": "restore the previous value of the changed configmap key and restart the deployment",
    "rollback_deployment": "kubectl rollout undo the deployment to its previous revision (image, probes, limits)",
    "scale_up": "increase the replica count of the deployment",
    "restart_deployment": "rollout restart the deployment",
    "raise_memory_limit": "increase the container memory limit",
    "remove_network_policy": "delete the NetworkPolicy that blocks traffic to the component",
    "none": "no action needed",
}

DIFFICULTIES = ["L1", "L2", "L3"]
