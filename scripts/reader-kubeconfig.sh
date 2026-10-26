#!/usr/bin/env bash
# Writes a kubeconfig that authenticates as one of the agent's ServiceAccounts instead of the admin.
#   bash scripts/reader-kubeconfig.sh .kube/reader.yaml                         (read-only, MCP tools)
#   bash scripts/reader-kubeconfig.sh .kube/remediator.yaml incidentpilot-remediator deploy/agent/remediator-rbac.yaml
set -euo pipefail

OUT="${1:-.kube/reader.yaml}"
SA="${2:-incidentpilot-reader}"
RBAC="${3:-deploy/agent/reader-rbac.yaml}"
mkdir -p "$(dirname "$OUT")"

kubectl apply -f "$RBAC" >/dev/null
for _ in $(seq 1 20); do
  TOKEN=$(kubectl -n shop get secret "$SA-token" -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
  [ -n "$TOKEN" ] && break
  sleep 1
done
[ -n "$TOKEN" ] || { echo "token not ready" >&2; exit 1; }

SERVER=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

cat > "$OUT" <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: incidentpilot
    cluster: {server: "$SERVER", certificate-authority-data: "$CA"}
users:
  - name: $SA
    user: {token: "$TOKEN"}
contexts:
  - name: $SA
    context: {cluster: incidentpilot, user: $SA, namespace: shop}
current-context: $SA
EOF
chmod 600 "$OUT"
echo "wrote $OUT"
