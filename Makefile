# Every check that runs in CI is defined here and nowhere else.
#
# The CI workflow invokes these same targets, so `make check` locally is the same
# gate a pull request faces. There is no second copy of the commands to drift.
#
# A tool that is not installed is skipped with a warning, so this is useful on a
# laptop without Docker. CI sets REQUIRE_ALL=1, which turns every skip into a
# failure, so a missing tool can never quietly pass in CI.

SHELL := /bin/sh

VENV    ?= .venv
PY      ?= $(VENV)/bin/python
PIP     ?= $(VENV)/bin/pip
CHART   ?= charts/acinfinity-exporter
IMAGE   ?= prometheus-acinfinity-pro69:dev

# Read from the package rather than duplicated here, so the smoke test asserts the
# image really carries the version this working tree claims.
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' src/acinfinity_exporter/__init__.py)

# Values every `helm template` invocation needs to satisfy the chart's own guards.
HELM_MIN := --set acinfinity.email=user@example.com --set acinfinity.password=secret

.DEFAULT_GOAL := help

define missing
if [ -n "$(REQUIRE_ALL)" ]; then \
  echo "ERROR: $(1) is required but not installed" >&2; exit 1; \
else \
  echo "SKIP: $(1) not installed ($(2))"; \
fi
endef

.PHONY: help
help:
	@echo "targets:"
	@echo "  setup           create $(VENV) and install with dev extras"
	@echo "  lint            ruff check + ruff format --check"
	@echo "  fmt             apply ruff formatting and autofixes"
	@echo "  test            pytest"
	@echo "  actionlint      lint the GitHub workflows"
	@echo "  actions-pinned  every uses: is SHA-pinned with a version comment"
	@echo "  helm            helm lint, template permutations, required values, kubeconform"
	@echo "  docker          hadolint, image build, smoke test"
	@echo "  scan            grype CVE scan of the built image"
	@echo "  check           everything above except scan"
	@echo "  clean           remove the venv and caches"

# ---------------------------------------------------------------- python ----

.PHONY: setup
setup:
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip >/dev/null
	$(PIP) install -e '.[dev]'

.PHONY: lint
lint:
	@echo "==> ruff check"
	$(VENV)/bin/ruff check src tests scripts
	@echo "==> ruff format --check"
	$(VENV)/bin/ruff format --check src tests scripts

.PHONY: fmt
fmt:
	$(VENV)/bin/ruff format src tests scripts
	$(VENV)/bin/ruff check --fix src tests scripts

.PHONY: test
test:
	@echo "==> pytest"
	$(VENV)/bin/pytest

# ------------------------------------------------------------- workflows ----

.PHONY: actionlint
actionlint:
	@if ! command -v actionlint >/dev/null 2>&1; then $(call missing,actionlint,brew install actionlint); else \
		echo "==> actionlint"; actionlint; \
	fi

# Every `uses:` must be pinned to a full commit SHA with a trailing version
# comment. A tag is mutable; a SHA is not.
.PHONY: actions-pinned
actions-pinned:
	@echo "==> actions pinned"
	@bad="$$(grep -rhn 'uses:' .github/workflows/ \
		| grep -vE 'uses: [^@]+@[0-9a-f]{40} # v?[0-9]' || true)"; \
	if [ -n "$$bad" ]; then \
		echo "unpinned or uncommented action references:"; echo "$$bad"; exit 1; \
	fi; echo "    every uses: is SHA-pinned with a version comment"

# ------------------------------------------------------------------ helm ----

.PHONY: helm
helm: helm-lint helm-template helm-required kubeconform

.PHONY: helm-lint
helm-lint:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,brew install helm); else \
		echo "==> helm lint"; helm lint $(CHART) $(HELM_MIN); \
	fi

# Four permutations: --set with defaults, an existing Secret, a values FILE
# (which takes a different YAML parser than --set), and the backfill Job.
.PHONY: helm-template
helm-template:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,helm template); else \
		set -e; echo "==> helm template (defaults)"; \
		helm template t $(CHART) $(HELM_MIN) >/dev/null; \
		echo "==> helm template (existing secret + scrape CRs)"; \
		helm template t $(CHART) --set acinfinity.existingSecret=s \
			--set vmServiceScrape.enabled=true --set serviceMonitor.enabled=true >/dev/null; \
		echo "==> helm template (values file)"; \
		out="$$(helm template t $(CHART) -f tests/helm/values-file.yaml)"; \
		echo "$$out" | grep -q 'ACINFINITY_EXPORTER_POLL_INTERVAL: "60"' \
			|| { echo "FAIL: poll interval did not render as an integer string"; exit 1; }; \
		echo "$$out" | grep -q 'Closet exhaust T10' \
			|| { echo "FAIL: portNames did not reach the ConfigMap"; exit 1; }; \
		echo "$$out" | grep -q 'kind: Job' \
			|| { echo "FAIL: backfill Job did not render"; exit 1; }; \
		echo "$$out" | grep -q 'enableServiceLinks: false' \
			|| { echo "FAIL: enableServiceLinks must be false"; exit 1; }; \
		echo "    values-file permutation rendered the expected keys"; \
	fi

# The chart must refuse to render without a credential source.
.PHONY: helm-required
helm-required:
	@if ! command -v helm >/dev/null 2>&1; then $(call missing,helm,helm required-values check); else \
		echo "==> helm required values"; \
		if helm template t $(CHART) >/dev/null 2>&1; then \
			echo "FAIL: chart rendered with no credential"; exit 1; fi; \
		echo "    refuses to render without acinfinity.email/password or existingSecret"; \
	fi

.PHONY: kubeconform
kubeconform:
	@if ! command -v kubeconform >/dev/null 2>&1; then $(call missing,kubeconform,brew install kubeconform); else \
		echo "==> kubeconform"; \
		helm template t $(CHART) $(HELM_MIN) --set backfill.enabled=true \
			--set backfill.since=2026-08-01T00:00:00Z --set backfill.victoriaMetricsUrl=http://vm:8428/api/v1/import/prometheus \
			| kubeconform -strict -summary -schema-location default \
				-skip ServiceMonitor,VMServiceScrape; \
	fi

# ---------------------------------------------------------------- docker ----

.PHONY: docker
docker: docker-lint docker-build docker-smoke

.PHONY: docker-lint
docker-lint:
	@if ! command -v hadolint >/dev/null 2>&1; then $(call missing,hadolint,brew install hadolint); else \
		echo "==> hadolint"; hadolint --failure-threshold warning Dockerfile; \
	fi

.PHONY: docker-build
docker-build:
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,docker build); else \
		set -e; echo "==> docker build"; docker build $(DOCKER_BUILD_ARGS) -t $(IMAGE) .; \
	fi

.PHONY: docker-smoke
docker-smoke:
	@if ! command -v docker >/dev/null 2>&1; then $(call missing,docker,image smoke test); else \
		set -e; echo "==> image smoke test"; \
		docker image inspect $(IMAGE) >/dev/null \
			|| { echo "FAIL: $(IMAGE) not built; run 'make docker-build' first"; exit 1; }; \
		docker run --rm $(IMAGE) --version | grep -q "$(VERSION)"; \
		echo "    reports version $(VERSION)"; \
		docker run --rm --entrypoint acinfinity-backfill $(IMAGE) --version | grep -q "$(VERSION)"; \
		echo "    backfill entrypoint is present"; \
		docker run --rm --read-only --tmpfs /tmp $(IMAGE) --version >/dev/null; \
		echo "    starts with a READ-ONLY rootfs (+ tmpfs /tmp), as the chart deploys it"; \
		out="$$(docker run --rm $(IMAGE) 2>&1 || true)"; \
		case "$$out" in \
			*"credentials are required"*) echo "    refuses to start unconfigured, with the expected message";; \
			*) echo "FAIL: unconfigured run said: $$out"; exit 1;; \
		esac; \
		if docker run --rm $(IMAGE) >/dev/null 2>&1; then \
			echo "FAIL: expected a non-zero exit with no configuration"; exit 1; fi; \
		echo "    runs as uid $$(docker run --rm --entrypoint python $(IMAGE) -c 'import os;print(os.getuid())')"; \
	fi

.PHONY: scan
scan:
	@if ! command -v grype >/dev/null 2>&1; then $(call missing,grype,brew install grype); else \
		set -e; echo "==> grype"; \
		docker image inspect $(IMAGE) >/dev/null 2>&1 \
			|| { echo "FAIL: $(IMAGE) not built; run 'make docker' first"; exit 1; }; \
		grype $(IMAGE) --only-fixed --fail-on high; \
	fi

# ----------------------------------------------------------------- gates ----

.PHONY: check
check: lint actionlint actions-pinned test helm docker
	@echo
	@echo "All available checks passed."
	@if [ -z "$(REQUIRE_ALL)" ]; then \
		echo "Note: anything reported as SKIP above was not run."; fi

.PHONY: clean
clean:
	rm -rf $(VENV) .pytest_cache .ruff_cache dist build *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
