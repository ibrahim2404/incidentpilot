#!/usr/bin/env bash
# Writes .kube/reader.yaml: a kubeconfig that authenticates as the read-only
# incidentpilot-reader ServiceAccount instead of the cluster admin.
set -euo pipefail

OUT="${1:-.kube/reader.yaml}"
mkdir -p "$(dirname "$OUT")"

kubectl apply -f deploy/agent/reader-rbac.yaml >/dev/null
for _ in $(seq 1 20); do
  TOKEN=$(kubectl -n shop get secret incidentpilot-reader-token -o jsonpath='{.data.token}' 2>/dev/null | base64 -d || true)
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
  - name: incidentpilot-reader
    user: {token: "$TOKEN"}
contexts:
  - name: reader
    context: {cluster: incidentpilot, user: incidentpilot-reader, namespace: shop}
current-context: reader
EOF
chmod 600 "$OUT"
echo "wrote $OUT"
