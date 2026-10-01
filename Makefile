CLUSTER ?= incidentpilot
IMAGE   ?= incidentpilot/shop:dev
PY      ?= python3

.PHONY: help up build build-k3s deploy wait test scenario prom alerts down

help:
	@echo "make up        create the k3d cluster"
	@echo "make build     build the shop image and import it into k3d"
	@echo "make build-k3s build the shop image and import it into k3s (Azure VM)"
	@echo "make deploy    apply monitoring + shop, wait for rollouts"
	@echo "make test      unit tests + scenario validation (no cluster needed)"
	@echo "make scenario S=S001   inject, wait for alert, save incident, revert"
	@echo "make prom      Prometheus UI on http://localhost:9090"
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

deploy:
	kubectl apply -f deploy/monitoring/
	kubectl apply -f deploy/shop/
	$(MAKE) wait

wait:
	kubectl -n monitoring rollout status deploy/prometheus --timeout=180s
	kubectl -n monitoring rollout status deploy/kube-state-metrics --timeout=180s
	for d in inventory orders gateway loadgen; do kubectl -n shop rollout status deploy/$$d --timeout=180s; done

test:
	cd apps/shop && $(PY) -m unittest -q
	$(PY) scenarios/runner.py validate

scenario:
	$(PY) scenarios/runner.py run $(S)

prom:
	kubectl -n monitoring port-forward svc/prometheus 9090:9090

alerts:
	$(PY) scenarios/runner.py alerts

down:
	k3d cluster delete $(CLUSTER)
