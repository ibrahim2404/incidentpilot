# IncidentPilot

An AI agent that responds to production alerts. It reads metrics, Kubernetes state and
logs through custom MCP servers, identifies the likely root cause and proposes a fix.
A human approves before anything runs.

Status: **milestone 1 — test environment**

## Layout
```
apps/shop/          demo service (gateway -> orders -> inventory), stdlib Python
deploy/shop/        demo app manifests + load generator
deploy/monitoring/  plain Prometheus, alert rules, kube-state-metrics
infra/k3d/          local single-node cluster
infra/kind/         CI cluster
infra/wsl/          WSL2 memory cap (Windows)
scenarios/          failure scenarios with ground truth + runner
docs/decisions.md   design decisions and deviations from the brief
```

## Quick start (Linux, macOS, or Windows via WSL2)
Requirements: Docker Engine, k3d, kubectl, Python 3.11+ with `pyyaml`.
```
make test                 # no cluster needed
make up build deploy      # ~1.5 GB RAM total
make scenario S=S002      # inject -> wait for alert -> save runs/S002-*.json -> revert
make prom                 # http://localhost:9090
make down
```

## Scenarios
| ID | Symptom alert | Root cause (component / fault) |
|----|---------------|--------------------------------|
| S001 | ContainerRestarting | orders / memory_leak |
| S002 | HighErrorRate | inventory / dependency_down |
| S003 | HighErrorRate | gateway / bad_config |
