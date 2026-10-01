# Design decisions

Changes to the kickoff brief, and why. Update this file whenever a decision changes.

## D1. Two evaluation tiers: replay on every PR, live cluster nightly
GitHub Models' free tier allows roughly 50 requests/day on "high" models and about
8k input tokens per request. A 50-scenario live eval with a multi-step agent needs
hundreds of calls, so it cannot run on every push.
- **Replay eval (every PR):** each scenario run is recorded once (alert payload plus
  Prometheus, Kubernetes and log snapshots). The MCP servers get a `fixture` backend that
  serves those snapshots. It is deterministic, fast, and needs no cluster.
- **Live eval (nightly / manual):** kind cluster, real fault injection, and a smaller
  sample if the quota is tight.
Consequence: every MCP server has to be written against a backend interface
(`live` | `fixture`) from the first day.

## D2. Structured diagnosis, deterministic scoring
The agent returns `{component, fault, evidence[], confidence}`, with `fault` taken from a
fixed taxonomy (`scenarios/runner.py: FAULTS`). Accuracy is an exact match on
component + fault. An LLM judge is used only to grade the free-text explanation, and it
is reported separately.

## D3. No answer leakage
Namespaces, labels, deployment names and config keys are neutral. Faults are real
misconfigurations (for example, a cache with no eviction), not flags named `LEAK=1`. The
agent receives the alert only. Ground truth stays in the scenario file.

## D4. Symptom alerts only
Alerts (`HighErrorRate`, `ContainerRestarting`, `NoTraffic`) describe symptoms. The
scenarios are chosen so that the alerting service is often not the root cause
(S002: the alert fires on orders and gateway, but the cause is inventory).

## D5. Logs come from the Kubernetes API, not Loki
This saves RAM. The logs MCP server reads pod logs, including `--previous` for
crashed containers. Loki can be added later if needed.

## D6. kube-state-metrics is included
It adds about 30 MB and exposes restart counts and `OOMKilled` termination reasons,
which the memory and crash scenarios need.

## D7. Tracing starts at milestone 3, not 5
Debugging a tool-using agent without traces is slow. Basic Langfuse spans are added
with the baseline agent. Cost and routing analysis stays in milestone 5.

## D8. Cost is reported at list price
Every call is free, so "cost" means tokens multiplied by the provider's public
per-token price. The README states this.

## D9. Report variance
Results use temperature 0 and N≥3 repetitions per scenario, reported as the mean and
range. A single run is noise.
