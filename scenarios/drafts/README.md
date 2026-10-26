# Draft scenarios (not in the golden set)

A scenario enters the golden set only when its fault reliably produces a user-visible symptom
(an alert). These did not, so they are kept here with what was learned instead of being forced.

## S018 inventory CPU limit 5m
Measured baseline: inventory uses ~5.6 millicores at 4 req/s. A 5m limit sits just under it.
In 3 live runs (420 s, 420 s, 600 s) no HighLatency or HighErrorRate fired: requests were slowed
but stayed under the 0.5 s p95 and the 2 s upstream timeout. A lower limit risks the opposite
failure: Python start-up (~0.1 CPU-s) would exceed the 30 s liveness grace, the new pod would be
killed before ready, and the rolling update would keep serving from the old pod, so again no symptom.
Next attempt: a two-step fault (limit at 5m plus a traffic increase) or a startupProbe-aware variant.
