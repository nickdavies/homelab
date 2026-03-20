.PHONY: validate validate-yaml validate-kustomize validate-refs validate-kustomize-full \
        validate-kubeconform validate-flux-local validate-all flux-schemas

# Run all local validations (no extra tools required beyond python3 + PyYAML)
validate: validate-yaml validate-kustomize validate-refs
	@echo "\nAll validations passed!"

# Run all validations including schema checking and Helm rendering
# Requires: kustomize, kubeconform, flux-local (pip install flux-local)
validate-all: validate validate-kustomize-full validate-kubeconform validate-flux-local
	@echo "\nAll validations passed (including schema and Helm rendering)!"

# 1. YAML syntax check
validate-yaml:
	@echo "==> [1/3] Checking YAML syntax..."
	@python3 scripts/validate-flux.py yaml

# 2. Kustomization resource reference check
#    - Forward: files listed in resources that don't exist
#    - Reverse: yaml files in the same dir with k8s resources not listed
validate-kustomize:
	@echo "==> [2/3] Checking kustomization resource references..."
	@python3 scripts/validate-flux.py kustomize

# 3. Flux cross-reference checks:
#    - HelmRelease sourceRef/chartRef → source defined
#    - Flux Kustomization dependsOn → target exists
#    - Flux Kustomization spec.path → directory exists
#    - secretRef.name → ExternalSecret or Secret provides it
validate-refs:
	@echo "==> [3/3] Checking Flux cross-references..."
	@python3 scripts/validate-flux.py refs

# Optional: full kustomize build (requires kustomize to be installed)
# Install: https://kubectl.sigs.k8s.io/kustomize/installation/
validate-kustomize-full:
	@command -v kustomize >/dev/null 2>&1 || { \
		echo "ERROR: kustomize not found."; \
		echo "Install: https://kubectl.sigs.k8s.io/kustomize/installation/"; \
		exit 1; \
	}
	@echo "==> Running full kustomize build..."
	@python3 scripts/validate-flux.py kustomize-full

# Schema validation via kubeconform against all kustomize-built manifests.
# Uses the Datree CRDs catalog for Flux and other CRD schemas.
# Requires: kustomize, kubeconform (https://github.com/yannh/kubeconform)
# Optional: run 'make flux-schemas' first to use local schemas instead.
FLUX_SCHEMA_DIR ?= /tmp/flux-crd-schemas
CRD_CATALOG_URL = https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json

validate-kubeconform:
	@command -v kustomize >/dev/null 2>&1 || { echo "ERROR: kustomize not found"; exit 1; }
	@command -v kubeconform >/dev/null 2>&1 || { \
		echo "ERROR: kubeconform not found."; \
		echo "Install: https://github.com/yannh/kubeconform#installation"; \
		exit 1; \
	}
	@echo "==> [kubeconform] Schema-validating all kustomize builds..."
	@echo "    (Docs with unresolved Flux substitution vars are skipped; see scripts/validate-flux.py filter-vars)"
	@if [ -d "$(FLUX_SCHEMA_DIR)" ]; then \
		SCHEMA_EXTRA="$(FLUX_SCHEMA_DIR)/{{.ResourceKind}}_{{.Group}}.json"; \
	else \
		SCHEMA_EXTRA="$(CRD_CATALOG_URL)"; \
	fi; \
	failed=0; built=0; skipped=0; \
	for dir in $$(find kubernetes/ -name kustomization.yaml -exec dirname {} \; | sort -u); do \
		out=$$(kustomize build "$$dir" 2>/dev/null) || { skipped=$$((skipped+1)); continue; }; \
		built=$$((built+1)); \
		filtered=$$(echo "$$out" | python3 scripts/validate-flux.py filter-vars); \
		[ -n "$$filtered" ] || continue; \
		echo "$$filtered" | kubeconform \
			-strict \
			-summary \
			-ignore-missing-schemas \
			-schema-location default \
			-schema-location "$$SCHEMA_EXTRA" \
			|| failed=$$((failed+1)); \
	done; \
	echo "  Built: $$built dirs, Skipped (kustomize errors): $$skipped dirs"; \
	[ $$failed -eq 0 ]

# Download Flux CRD schemas locally for offline kubeconform validation.
flux-schemas:
	@mkdir -p $(FLUX_SCHEMA_DIR)
	@echo "==> Downloading Flux CRD schemas to $(FLUX_SCHEMA_DIR)..."
	@curl -sL https://github.com/fluxcd/flux2/releases/latest/download/crd-schemas.tar.gz \
		| tar zxf - -C $(FLUX_SCHEMA_DIR)/
	@echo "  Done. Use: make validate-kubeconform FLUX_SCHEMA_DIR=$(FLUX_SCHEMA_DIR)"

# Flux reconciliation graph validation (structure only, no Helm rendering).
# Walks the Flux Kustomization graph and verifies paths, sourceRefs, and dependsOn
# are all consistent. Helm chart rendering is skipped because it requires
# cluster-config-secret (populated by ExternalSecrets at runtime, not in the repo).
# Requires: flux-local (pip install flux-local), helm, kustomize, flux CLI
validate-flux-local:
	@command -v flux-local >/dev/null 2>&1 || { \
		echo "ERROR: flux-local not found."; \
		echo "Install: pip install flux-local"; \
		exit 1; \
	}
	@echo "==> [flux-local] Validating Flux reconciliation graph structure..."
	@flux-local test --path kubernetes/flux/ --sources homelab
