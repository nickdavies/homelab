#!/usr/bin/env python3
"""Local Flux/Kustomize validation script.

Usage:
  validate-flux.py yaml             - Check YAML syntax in kubernetes/
  validate-flux.py kustomize        - Check kustomization resource references
  validate-flux.py refs             - Check Flux cross-references
  validate-flux.py kustomize-full   - Run kustomize build (requires kustomize)
"""

import argparse
import os
import sys
import glob
import subprocess
from pathlib import Path
from typing import Iterator

try:
    import yaml
    from yaml.constructor import ConstructorError
    from yaml.composer import ComposerError
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml")
    sys.exit(1)


def _make_loader() -> type:
    """Return a SafeLoader that handles Go-YAML-compatible quirks:
    - `=` as a plain scalar (YAML 1.1 merge indicator treated as a value by Go's parser)
    - Duplicate anchors (Go yaml silently overwrites; Python raises ComposerError)
    """
    class PermissiveSafeLoader(yaml.SafeLoader):
        pass

    # Handle `tag:yaml.org,2002:value` (the bare `=` scalar) — return it as "="
    PermissiveSafeLoader.add_constructor(
        "tag:yaml.org,2002:value",
        lambda loader, node: loader.construct_scalar(node),
    )

    return PermissiveSafeLoader


LOADER = _make_loader()

REPO_ROOT = Path(__file__).parent.parent
KUBERNETES_DIR = REPO_ROOT / "kubernetes"

errors: list[str] = []


def err(msg: str) -> None:
    errors.append(msg)
    print(f"  ERROR: {msg}")


def rel(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT))


# ---------------------------------------------------------------------------
# YAML loading helpers
# ---------------------------------------------------------------------------

def load_docs(path: Path) -> list[dict]:
    """Load all YAML documents from a file, silently skipping parse errors.

    Parse errors are intentionally swallowed here because load_docs is used
    by the cross-reference and kustomize checks, which operate on already-valid
    files. YAML syntax errors are reported separately by validate_yaml_syntax(),
    so callers of load_docs can assume that broken files simply return no docs.
    """
    try:
        with open(path) as f:
            return [d for d in yaml.load_all(f.read(), Loader=LOADER) if isinstance(d, dict)]
    except yaml.YAMLError:
        return []


def all_yaml_files(base: Path = KUBERNETES_DIR) -> Iterator[Path]:
    yield from base.rglob("*.yaml")


# ---------------------------------------------------------------------------
# Stage 1: YAML syntax
# ---------------------------------------------------------------------------

def validate_yaml_syntax() -> None:
    count = 0
    for path in all_yaml_files():
        count += 1
        try:
            with open(path) as f:
                list(yaml.load_all(f.read(), Loader=LOADER))
        except ComposerError as e:
            # Duplicate YAML anchors are rejected by Python but accepted by Go's
            # yaml parser (used by Kubernetes/Flux). Warn but don't fail.
            if "found duplicate anchor" in str(e):
                print(f"  WARN: {rel(path)}: duplicate YAML anchor (OK for Kubernetes)")
            else:
                err(f"{rel(path)}: {e}")
        except yaml.YAMLError as e:
            err(f"{rel(path)}: {e}")
    print(f"  Checked {count} files")


# ---------------------------------------------------------------------------
# Stage 2: Kustomization resource references
# ---------------------------------------------------------------------------

def _collect_kustomization_files(kustomize_dir: Path, kust: dict) -> set[str]:
    """Collect all filenames/subpaths referenced in a kustomization spec."""
    referenced: set[str] = set()

    def _add(entries: list | None) -> None:
        for e in (entries or []):
            if isinstance(e, str):
                referenced.add(e)

    _add(kust.get("resources"))
    _add(kust.get("components"))
    _add(kust.get("crds"))

    # configMapGenerator / secretGenerator files
    for gen_list in [kust.get("configMapGenerator", []), kust.get("secretGenerator", [])]:
        for gen in (gen_list or []):
            for f in gen.get("files", []):
                # files entries can be "key=path" or just "path"
                referenced.add(f.split("=")[-1])

    # patches with paths
    for patch in kust.get("patches", []):
        if isinstance(patch, dict) and "path" in patch:
            referenced.add(patch["path"])

    return referenced


def validate_kustomize_resources() -> None:
    kust_files = list(KUBERNETES_DIR.rglob("kustomization.yaml"))
    print(f"  Checking {len(kust_files)} kustomization.yaml files")

    for kust_path in kust_files:
        kust_dir = kust_path.parent
        docs = load_docs(kust_path)
        if not docs:
            continue
        kust = docs[0]

        referenced = _collect_kustomization_files(kust_dir, kust)

        # Forward check: referenced paths that don't exist
        for ref in referenced:
            # Skip remote references: URLs and kustomize remote git references
            # (e.g. "github.com/foo/bar?ref=v1", "https://...", "oci://...")
            if (
                ref.startswith("http://")
                or ref.startswith("https://")
                or ref.startswith("oci://")
                or (not ref.startswith(".") and not ref.startswith("/") and "/" in ref and "." in ref.split("/")[0])
            ):
                continue
            target = (kust_dir / ref).resolve()
            if not target.exists():
                err(f"{rel(kust_path)}: resource '{ref}' does not exist")

        # Reverse check: yaml files in this dir not included in resources
        # Normalize resource paths to basenames so "./foo.yaml" matches "foo.yaml"
        resource_basenames: set[str] = set()
        for ref in referenced:
            # Refs with parent traversal or subdir paths are not bare files
            if ref.startswith("..") or (
                "/" in ref and not ref.startswith("./")
            ):
                continue
            # Strip leading "./"
            resource_basenames.add(ref.lstrip("./"))

        for candidate in kust_dir.glob("*.yaml"):
            if candidate.name == "kustomization.yaml":
                continue
            if candidate.name in resource_basenames:
                continue
            # Only flag if it looks like a Kubernetes manifest
            docs_in_candidate = load_docs(candidate)
            for doc in docs_in_candidate:
                if "apiVersion" in doc and "kind" in doc:
                    err(
                        f"{rel(kust_path)}: '{candidate.name}' has Kubernetes resources"
                        f" but is not listed in resources"
                    )
                    break


# ---------------------------------------------------------------------------
# Stage 3: Flux cross-reference validation
# ---------------------------------------------------------------------------

def _find_all_secret_refs(obj, refs: set[str]) -> None:
    """Recursively find all secretRef.name values in a YAML object."""
    if isinstance(obj, dict):
        if "secretRef" in obj and isinstance(obj["secretRef"], dict):
            name = obj["secretRef"].get("name", "")
            if name:
                refs.add(name)
        for v in obj.values():
            _find_all_secret_refs(v, refs)
    elif isinstance(obj, list):
        for item in obj:
            _find_all_secret_refs(item, refs)


def _find_envfrom_secret_refs(obj, refs: set[str]) -> None:
    """Find secretRef names inside envFrom lists."""
    if isinstance(obj, dict):
        if "envFrom" in obj and isinstance(obj["envFrom"], list):
            for entry in obj["envFrom"]:
                if isinstance(entry, dict) and "secretRef" in entry:
                    name = entry["secretRef"].get("name", "")
                    if name:
                        refs.add(name)
        for v in obj.values():
            _find_envfrom_secret_refs(v, refs)
    elif isinstance(obj, list):
        for item in obj:
            _find_envfrom_secret_refs(item, refs)


def validate_flux_refs() -> None:
    # Build indexes by scanning all YAML files
    flux_ks_names: dict[str, Path] = {}        # Flux Kustomization name → file
    sources: dict[tuple[str, str], Path] = {}  # (kind, name) → file
    provided_secrets: dict[str, Path] = {}     # secret name → file
    helm_releases: list[tuple[Path, dict]] = []
    flux_kustomizations: list[tuple[Path, dict]] = []

    for path in all_yaml_files():
        for doc in load_docs(path):
            kind = doc.get("kind", "")
            api = doc.get("apiVersion", "")
            name = doc.get("metadata", {}).get("name", "")

            # Collect Flux Kustomization objects
            if kind == "Kustomization" and "kustomize.toolkit.fluxcd.io" in api:
                if name:
                    flux_ks_names[name] = path
                flux_kustomizations.append((path, doc))

            # Collect source objects
            elif kind in ("HelmRepository", "OCIRepository", "GitRepository",
                          "Bucket", "HelmChart"):
                if name:
                    sources[(kind, name)] = path

            # Collect secret providers
            elif kind == "ExternalSecret":
                target = doc.get("spec", {}).get("target", {}).get("name", "")
                if target:
                    provided_secrets[target] = path
            elif kind == "Secret":
                if name:
                    provided_secrets[name] = path

            # Collect HelmReleases for later validation
            elif kind == "HelmRelease":
                helm_releases.append((path, doc))

    print(
        f"  Indexed: {len(flux_ks_names)} Flux Kustomizations, "
        f"{len(sources)} sources, {len(provided_secrets)} provided secrets, "
        f"{len(helm_releases)} HelmReleases"
    )

    # 3a. Validate HelmRelease sourceRef / chartRef
    for path, doc in helm_releases:
        spec = doc.get("spec", {})

        # New-style: chartRef
        chart_ref = spec.get("chartRef")
        if chart_ref and isinstance(chart_ref, dict):
            kind = chart_ref.get("kind", "")
            name = chart_ref.get("name", "")
            if kind and name and (kind, name) not in sources:
                err(
                    f"{rel(path)}: HelmRelease '{doc['metadata'].get('name')}' "
                    f"chartRef {kind}/{name} not found"
                )

        # Old-style: chart.spec.sourceRef
        chart_spec = spec.get("chart", {}).get("spec", {})
        source_ref = chart_spec.get("sourceRef", {})
        if source_ref:
            kind = source_ref.get("kind", "")
            name = source_ref.get("name", "")
            if kind and name and (kind, name) not in sources:
                err(
                    f"{rel(path)}: HelmRelease '{doc['metadata'].get('name')}' "
                    f"sourceRef {kind}/{name} not found"
                )

    # 3b. Validate Flux Kustomization dependsOn
    for path, doc in flux_kustomizations:
        ks_name = doc.get("metadata", {}).get("name", "")
        for dep in doc.get("spec", {}).get("dependsOn", []):
            dep_name = dep.get("name", "")
            if dep_name and dep_name not in flux_ks_names:
                err(
                    f"{rel(path)}: Flux Kustomization '{ks_name}' "
                    f"dependsOn '{dep_name}' which does not exist"
                )

    # 3c. Validate Flux Kustomization spec.path exists
    # Only validate paths for Kustomizations pointing at the local repo (homelab),
    # not external private repos (homelab-data, hass-configs) whose paths live
    # in a different git repository.
    LOCAL_REPO_NAME = "homelab"
    for path, doc in flux_kustomizations:
        spec = doc.get("spec", {})
        source_ref = spec.get("sourceRef", {})
        if source_ref.get("name", "") != LOCAL_REPO_NAME:
            continue
        ks_name = doc.get("metadata", {}).get("name", "")
        ks_path_str = spec.get("path", "")
        if not ks_path_str:
            continue
        # Paths are relative to repo root, prefixed with ./
        ks_path = (REPO_ROOT / ks_path_str.lstrip("./")).resolve()
        if not ks_path.is_dir():
            err(
                f"{rel(path)}: Flux Kustomization '{ks_name}' "
                f"spec.path '{ks_path_str}' does not exist"
            )

    # 3d. Validate secret references
    for path in all_yaml_files():
        for doc in load_docs(path):
            kind = doc.get("kind", "")
            # Only check resource types that reference secrets
            if kind not in ("HelmRelease", "Deployment", "StatefulSet",
                            "DaemonSet", "GitRepository", "Pod"):
                continue

            secret_ref_names: set[str] = set()
            _find_all_secret_refs(doc, secret_ref_names)
            _find_envfrom_secret_refs(doc, secret_ref_names)

            for secret_name in secret_ref_names:
                # Skip Flux substitution variables like ${APP}-secret
                if "${" in secret_name:
                    continue
                if secret_name not in provided_secrets:
                    resource_name = doc.get("metadata", {}).get("name", "?")
                    err(
                        f"{rel(path)}: {kind} '{resource_name}' references "
                        f"secret '{secret_name}' but no ExternalSecret or "
                        f"Secret provides it"
                    )


# ---------------------------------------------------------------------------
# Stage 4: Full kustomize build (optional)
# ---------------------------------------------------------------------------

def validate_kustomize_build() -> None:
    kust_files = list(KUBERNETES_DIR.rglob("kustomization.yaml"))
    print(f"  Running kustomize build on {len(kust_files)} directories")
    failed = 0
    for kust_path in kust_files:
        kust_dir = kust_path.parent
        result = subprocess.run(
            ["kustomize", "build", str(kust_dir)],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            failed += 1
            # Trim verbose kustomize output to first 5 lines
            output = "\n    ".join(result.stderr.strip().splitlines()[:5])
            err(f"{rel(kust_path)}: kustomize build failed:\n    {output}")
    if not failed:
        print(f"  All {len(kust_files)} kustomize builds succeeded")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

STAGES = {
    "yaml": ("Checking YAML syntax", validate_yaml_syntax),
    "kustomize": ("Checking kustomization resource references", validate_kustomize_resources),
    "refs": ("Checking Flux cross-references", validate_flux_refs),
    "kustomize-full": ("Running full kustomize build", validate_kustomize_build),
}


# ---------------------------------------------------------------------------
# filter-vars: stdin filter for kubeconform pre-processing
# ---------------------------------------------------------------------------

def _has_flux_var(obj: object) -> bool:
    """Return True if any string value in the object contains a Flux
    postBuild substitution variable like ${APP} or ${SECRET_DOMAIN}."""
    if isinstance(obj, str):
        return "${" in obj
    if isinstance(obj, dict):
        return any(_has_flux_var(v) for v in obj.values())
    if isinstance(obj, list):
        return any(_has_flux_var(item) for item in obj)
    return False


import re as _re

_DIGIT_ONLY = _re.compile(r'^\d+$')


def _make_kubeconform_dumper() -> type:
    """SafeDumper that double-quotes digit-only strings.

    Without this, PyYAML emits strings like '0380' unquoted.  Go yaml.v2
    (used internally by kubeconform) then parses unquoted '0380' as integer
    380 via decimal fallback after a failed octal parse — causing spurious
    "got number, want string" schema failures (e.g. NodeFeatureRule PCI
    class codes such as "0300"/"0380").
    """
    class _Dumper(yaml.SafeDumper):
        pass

    def _str_representer(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
        if _DIGIT_ONLY.match(data):
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style='"')
        return yaml.SafeDumper.represent_str(dumper, data)  # type: ignore[attr-defined]

    _Dumper.add_representer(str, _str_representer)
    return _Dumper


_KUBECONFORM_DUMPER = _make_kubeconform_dumper()


def filter_flux_vars_stdin() -> None:
    """Read multi-doc YAML from stdin and write only documents that contain
    no Flux postBuild substitution variables (${...}) to stdout.

    Used as a pre-filter before piping kustomize build output to kubeconform:
    resources with unresolved variables fail kubeconform's pattern validators
    (hostname patterns, quantity patterns, cron patterns) by design — they are
    valid manifests whose variables are substituted at runtime by Flux.

    The custom dumper also double-quotes digit-only strings to prevent YAML 1.1
    parsers (Go yaml.v2) from misinterpreting bare digit strings as integers.
    """
    content = sys.stdin.read()
    try:
        docs = [
            d for d in yaml.load_all(content, Loader=LOADER)
            if isinstance(d, dict) and not _has_flux_var(d)
        ]
    except yaml.YAMLError:
        docs = []
    if docs:
        sys.stdout.write(yaml.dump_all(docs, Dumper=_KUBECONFORM_DUMPER, default_flow_style=False))


if __name__ == "__main__":
    all_modes = list(STAGES.keys()) + ["filter-vars"]
    parser = argparse.ArgumentParser(description="Local Flux/Kustomize validation script.")
    parser.add_argument(
        "mode",
        choices=all_modes,
        help="Validation stage to run. Use 'filter-vars' as a stdin→stdout pre-filter for kubeconform.",
    )
    args = parser.parse_args()

    # filter-vars is a stdin→stdout filter, not a normal validation stage
    if args.mode == "filter-vars":
        filter_flux_vars_stdin()
        sys.exit(0)

    label, fn = STAGES[args.mode]
    print(f"{label}...")
    fn()

    if errors:
        print(f"\nFound {len(errors)} error(s). Fix the above issues and re-run.")
        sys.exit(1)
    else:
        print("  OK")
