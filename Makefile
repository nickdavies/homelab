.PHONY: validate validate-yaml validate-kustomize validate-refs validate-kustomize-full

# Run all local validations (no extra tools required beyond python3 + PyYAML)
validate: validate-yaml validate-kustomize validate-refs
	@echo "\nAll validations passed!"

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
