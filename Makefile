CLUSTER ?= incidentpilot
IMAGE   ?= incidentpilot/shop:dev
PY      ?= python3

.PHONY: help up build build-k3s deploy wait test scenario prom alerts down reader-kubeconfig incident replay-tools record golden eval

help:
	@echo "make up        create the k3d cluster"
	@echo "make build     build the shop image and import it into k3d"
	@echo "make build-k3s build the shop image and import it into k3s (Azure VM)"
	@echo "make deploy    apply monitoring + shop, wait for rollouts"
	@echo "make test      unit tests + scenario validation (no cluster needed)"
	@echo "make scenario S=S001   inject, wait for alert, save incident, revert"
	@echo "make prom      Prometheus UI on http://localhost:9090"
	@echo "make reader-kubeconfig   read-only kubeconfig for the MCP servers (.kube/reader.yaml)"
	@echo "make down      delete the cluster"

up:
	k3d cluster create --config infra/k3d/cluster.yaml

build:
	docker build -t $(IMAGE) apps/shop
	k3d image import $(IMAGE) -c $(CLUSTER)

# On the Azure VM (plain k3s, no k3d): load the image into k3s's containerd.
build-k3s:
	docker build -t $(IMAGE) apps/shop
	docker save $(IMAGE) | sudo k3s ctr images import -

# Restarts are needed: Prometheus only reads its config and rules at start,
# and pods keep the old image when a new one is loaded under the same tag.
deploy:
	kubectl apply -f deploy/monitoring/
	kubectl apply -f deploy/shop/
	kubectl apply -f deploy/agent/reader-rbac.yaml
	kubectl -n monitoring rollout restart deploy/prometheus
	kubectl -n shop rollout restart deploy/gateway deploy/orders deploy/inventory
	$(MAKE) wait

wait:
	kubectl -n monitoring rollout status deploy/prometheus --timeout=180s
	kubectl -n monitoring rollout status deploy/kube-state-metrics --timeout=180s
	for d in inventory orders gateway loadgen; do kubectl -n shop rollout status deploy/$$d --timeout=180s; done

test:
	cd apps/shop && $(PY) -m unittest -q
	$(PY) -m unittest discover -q -s mcp_servers/prometheus
	$(PY) -m unittest discover -q -s mcp_servers/kube
	$(PY) -m unittest discover -q -s agent
	$(PY) scenarios/runner.py validate

reader-kubeconfig:
	bash scripts/reader-kubeconfig.sh .kube/reader.yaml

scenario:
	$(PY) scenarios/runner.py run $(S)

prom:
	kubectl -n monitoring port-forward svc/prometheus 9090:9090

alerts:
	$(PY) scenarios/runner.py alerts

down:
	k3d cluster delete $(CLUSTER)

# Full live loop for one scenario: inject + wait for alert, diagnose, always revert.
# make incident ID=S002
incident:
	$(PY) scenarios/runner.py run $(ID) --keep
	$(PY) agent/run.py --incident $$(ls -t runs/$(ID)-*.json | head -1) || true
	$(PY) scenarios/runner.py revert $(ID)

# prometheus + promtool binaries used to replay recorded incidents (no cluster needed)
PROM_VERSION ?= 2.55.1
replay-tools:
	mkdir -p .bin && curl -sSL https://github.com/prometheus/prometheus/releases/download/v$(PROM_VERSION)/prometheus-$(PROM_VERSION).linux-amd64.tar.gz \
	  | tar xz -C .bin --strip-components=1 prometheus-$(PROM_VERSION).linux-amd64/prometheus prometheus-$(PROM_VERSION).linux-amd64/promtool
	.bin/promtool --version | head -1

# Record one incident into golden/ (inject, wait for alert, freeze metrics/kube/logs, revert)
# make record ID=S002
record:
	$(PY) scenarios/runner.py run $(ID) --record

# Record every scenario (or IDS="S006 S007"), one after the other. A scenario whose alert does
# not fire is reverted, logged in golden/failures.log, and the loop moves on.
IDS ?= $(shell $(PY) scenarios/runner.py list | cut -d' ' -f1)
golden:
	mkdir -p golden
	for s in $(IDS); do \
	  $(PY) scenarios/runner.py run $$s --record || echo "$$(date -u +%FT%TZ) $$s failed" >> golden/failures.log; \
	done
	@echo "recorded: $$(ls -d golden/S* 2>/dev/null | wc -l) incidents"; cat golden/failures.log 2>/dev/null || true

# Replay evaluation on golden/. make eval ARGS="--repeats 1 --only S002"
eval:
	$(PY) agent/evaluate.py $(ARGS)
