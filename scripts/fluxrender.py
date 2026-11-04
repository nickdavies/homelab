"""Build the repo the way Flux would, and render its HelmReleases.

Shared by chart-diff.py and crd-order.py:

  - load_tree() kustomize-builds every Flux Kustomization's path, applying the
    postBuild substitutions Flux would (cluster-settings and the
    Kustomization's own `substitute`; variables only the cluster has become
    PLACEHOLDER_<NAME>), and records which Flux Kustomization applies each
    object.
  - releases() resolves each HelmRelease's chart and values from the repo.
  - render() runs `helm template` for one release, with every Secret value
    replaced by REDACTED; split() keys the output.

Requires: kustomize, helm, PyYAML.
"""

import hashlib
import re
import subprocess
import sys
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml")
    sys.exit(1)

FLUX_KS_API = "kustomize.toolkit.fluxcd.io/"
SUBSTITUTE_DISABLED = "kustomize.toolkit.fluxcd.io/substitute"
NO_SUBSTITUTION_LABEL = "substitution.flux.home.arpa/disabled"
# drone/envsubst syntax, the subset Flux documents: ${VAR}, ${VAR:=default},
# ${VAR:-default}, ${VAR=default}, ${VAR-default}.
VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?:(:?[-=])([^}]*))?\}")


class Loader(yaml.SafeLoader):
    """Go-yaml compatible: a bare `=` is a string."""


Loader.add_constructor("tag:yaml.org,2002:value", lambda l, n: l.construct_scalar(n))


def load_all(text: str) -> list[dict]:
    return [d for d in yaml.load_all(text, Loader=Loader) if isinstance(d, dict)]


def dump(obj) -> str:
    return yaml.safe_dump(obj, sort_keys=True, default_flow_style=False, width=1000)


def meta(obj: dict) -> dict:
    return obj.get("metadata") or {}


# ---------------------------------------------------------------------------
# Building a checkout the way kustomize-controller would
# ---------------------------------------------------------------------------

@dataclass
class Tree:
    root: Path
    objects: dict = field(default_factory=dict)   # (kind, ns, name) -> obj
    placeholders: dict = field(default_factory=dict)  # (kind, ns, name) -> set of vars
    owner: dict = field(default_factory=dict)     # (kind, ns, name) -> Flux Kustomization name
    applied: dict = field(default_factory=dict)   # Flux Kustomization name -> [obj]
    build_errors: list = field(default_factory=list)

    def get(self, kind: str, ns: str, name: str):
        return self.objects.get((kind, ns, name))


def flux_kustomizations(root: Path) -> list[dict]:
    """The Flux Kustomizations that build from this repo; one whose sourceRef
    is another GitRepository builds that repo's tree, which isn't here."""
    found = []
    for path in sorted((root / "kubernetes").rglob("*.yaml")):
        try:
            docs = load_all(path.read_text())
        except yaml.YAMLError:
            continue
        for d in docs:
            if d.get("kind") == "Kustomization" and str(d.get("apiVersion", "")).startswith(FLUX_KS_API):
                spec = d.get("spec") or {}
                path_spec = spec.get("path")
                source = spec.get("sourceRef") or {}
                if source.get("kind") != "GitRepository" or source.get("name") != SOURCE:
                    continue
                if isinstance(path_spec, str) and "<<" not in path_spec:
                    found.append(d)
    return found


def substitute(text: str, values: dict, used: set) -> str:
    def repl(m: re.Match) -> str:
        name, op, default = m.group(1), m.group(2), m.group(3)
        if name in values:
            return str(values[name])
        if op is not None:
            return default
        used.add(name)
        return f"PLACEHOLDER_{name}"
    return VAR.sub(repl, text)


def generated(target: Path) -> tuple[list[Path], list[Path]]:
    """What kustomize-controller applies for a path with no kustomization.yaml:
    every subdirectory that has one, built whole, and every manifest outside
    those."""
    dirs, files = [], []
    for entry in sorted(target.iterdir()):
        if entry.is_dir():
            if (entry / "kustomization.yaml").exists():
                dirs.append(entry)
            else:
                d, f = generated(entry)
                dirs += d
                files += f
        elif entry.suffix in (".yaml", ".yml"):
            files.append(entry)
    return dirs, files


def build(root: Path, ks: str, path: str, target_ns: str | None, variables: dict | None, tree: Tree) -> None:
    """kustomize build one Flux Kustomization's path into tree.objects."""
    target = root / path
    if not target.is_dir():
        return
    dirs, files = ([target], []) if (target / "kustomization.yaml").exists() else generated(target)
    texts = [f.read_text() for f in files]
    for d in dirs:
        result = subprocess.run(["kustomize", "build", str(d)], capture_output=True, text=True)
        if result.returncode != 0:
            tree.build_errors.append(f"{d.relative_to(root)}: {result.stderr.strip().splitlines()[-1:]}")
            continue
        texts.append(result.stdout)
    for raw in re.split(r"^---\s*$", "\n---\n".join(texts), flags=re.M):
        if not raw.strip():
            continue
        try:
            docs = load_all(raw)
        except yaml.YAMLError:
            continue
        for obj in docs:
            if not (obj.get("apiVersion") and obj.get("kind")):
                continue
            used: set = set()
            annotations = meta(obj).get("annotations") or {}
            if variables is not None and annotations.get(SUBSTITUTE_DISABLED) != "disabled":
                obj = load_all(substitute(raw, variables, used))[0]
            if target_ns and obj.get("kind") in NAMESPACED:
                obj["metadata"]["namespace"] = target_ns
            key = (obj.get("kind"), meta(obj).get("namespace", ""), meta(obj).get("name"))
            tree.objects[key] = obj
            tree.owner[key] = ks
            tree.applied.setdefault(ks, []).append(obj)
            if used:
                tree.placeholders[key] = used


# Kinds the HelmRelease resolution looks up; kustomize-controller moves them
# into targetNamespace like any namespaced object.
NAMESPACED = {"HelmRelease", "HelmRepository", "OCIRepository", "ConfigMap", "Secret"}

# The GitRepository for this repo, and the Flux Kustomization the bootstrap
# creates from it to apply kubernetes/flux.
SOURCE = "homelab"
BOOTSTRAP_KS = "homelab"


def load_tree(root: Path) -> Tree:
    tree = Tree(root)
    settings: dict = {}
    for path in sorted((root / "kubernetes").rglob("*.yaml")):
        try:
            for d in load_all(path.read_text()):
                if d.get("kind") == "ConfigMap" and meta(d).get("name") == "cluster-settings":
                    settings.update({k: str(v) for k, v in (d.get("data") or {}).items()})
        except yaml.YAMLError:
            continue

    # The bootstrap Kustomization isn't in the repo; it builds kubernetes/flux
    # with no substitution.
    build(root, BOOTSTRAP_KS, "kubernetes/flux", None, None, tree)
    for ks in flux_kustomizations(root):
        spec = ks.get("spec") or {}
        labels = meta(ks).get("labels") or {}
        post = spec.get("postBuild") or {}
        # cluster-apps patches cluster-settings into every child Kustomization
        # that doesn't opt out; Flux substitutes only when postBuild is set.
        inherits = str(labels.get(NO_SUBSTITUTION_LABEL)) != "true"
        variables = None
        if inherits or post:
            variables = dict(settings) if inherits else {}
            variables.update({k: str(v) for k, v in (post.get("substitute") or {}).items()})
        build(root, meta(ks)["name"], spec["path"].lstrip("./"), spec.get("targetNamespace"), variables, tree)
    return tree


# ---------------------------------------------------------------------------
# Resolving a HelmRelease
# ---------------------------------------------------------------------------

@dataclass
class Chart:
    """What `helm pull` needs, and a label for humans."""
    ref: str            # repo chart name or oci:// reference
    repo: str | None    # --repo for a classic HelmRepository
    version: str | None
    label: str

    @property
    def identity(self) -> tuple:
        return (self.ref, self.repo, self.version)


@dataclass
class Release:
    namespace: str
    name: str
    release_name: str
    target_namespace: str
    chart: Chart | None
    values: dict
    notes: list
    crds_on_upgrade: str


def find_source(tree: Tree, kind: str, ns: str, name: str):
    obj = tree.get(kind, ns, name)
    if obj is None:
        matches = [o for (k, _, n), o in tree.objects.items() if k == kind and n == name]
        if len(matches) == 1:
            obj = matches[0]
    return obj


def resolve_chart(tree: Tree, hr: dict, ns: str, notes: list) -> Chart | None:
    spec = hr.get("spec") or {}
    if "chartRef" in spec:
        ref = spec["chartRef"]
        if ref.get("kind") != "OCIRepository":
            notes.append(f"chartRef kind {ref.get('kind')} isn't rendered")
            return None
        src = find_source(tree, "OCIRepository", ref.get("namespace", ns), ref["name"])
        if src is None:
            notes.append(f"OCIRepository {ref['name']} not found")
            return None
        url = src["spec"]["url"]
        oref = src["spec"].get("ref") or {}
        if oref.get("digest"):
            label = oref.get("tag") or oref["digest"][:19]
            return Chart(f"{url}@{oref['digest']}", None, None, f"{url} {label}")
        version = oref.get("tag") or oref.get("semver")
        return Chart(url, None, version, f"{url} {version}")

    cspec = (spec.get("chart") or {}).get("spec") or {}
    source = cspec.get("sourceRef") or {}
    if source.get("kind") != "HelmRepository":
        notes.append(f"chart source kind {source.get('kind')} isn't rendered")
        return None
    repo = find_source(tree, "HelmRepository", source.get("namespace", ns), source["name"])
    if repo is None:
        notes.append(f"HelmRepository {source['name']} not found")
        return None
    url = repo["spec"]["url"].rstrip("/")
    chart = cspec["chart"]
    version = str(cspec.get("version", "*"))
    if url.startswith("oci://") or repo["spec"].get("type") == "oci":
        return Chart(f"{url}/{chart}", None, version, f"{url}/{chart} {version}")
    return Chart(chart, url, version, f"{chart} {version} ({url})")


def merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def set_path(values: dict, path: str, value) -> None:
    parts = path.split(".")
    for p in parts[:-1]:
        values = values.setdefault(p, {})
    values[parts[-1]] = value


def resolve_values(tree: Tree, hr: dict, ns: str, notes: list) -> dict:
    spec = hr.get("spec") or {}
    values: dict = {}
    for ref in spec.get("valuesFrom") or []:
        kind, name = ref.get("kind"), ref.get("name")
        key = ref.get("valuesKey", "values.yaml")
        if kind == "Secret":
            notes.append(f"values from Secret {name} aren't in the repo; rendered without them")
            continue
        cm = tree.get("ConfigMap", ns, name)
        if cm is None or key not in (cm.get("data") or {}):
            notes.append(f"values from ConfigMap {name} not found in the repo; rendered without them")
            continue
        raw = cm["data"][key]
        if ref.get("targetPath"):
            set_path(values, ref["targetPath"], raw)
        else:
            values = merge(values, yaml.load(raw, Loader=Loader) or {})
    return merge(values, spec.get("values") or {})


def releases(tree: Tree) -> dict:
    out = {}
    for (kind, ns, name), hr in tree.objects.items():
        if kind != "HelmRelease":
            continue
        spec = hr.get("spec") or {}
        notes: list = []
        target_ns = spec.get("targetNamespace") or ns
        release_name = spec.get("releaseName") or (
            f"{spec['targetNamespace']}-{name}" if spec.get("targetNamespace") else name
        )
        chart = resolve_chart(tree, hr, ns, notes)
        values = resolve_values(tree, hr, ns, notes)
        placeholders = tree.placeholders.get((kind, ns, name))
        if placeholders:
            notes.append(
                "substituted from the cluster, rendered as placeholders: "
                + ", ".join(f"`{v}`" for v in sorted(placeholders))
            )
        crds = ((spec.get("upgrade") or {}).get("crds")) or "Skip"
        out[(ns, name)] = Release(ns, name, release_name, target_ns, chart, values, notes, crds)
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def pull(chart: Chart, cache: Path) -> Path:
    """Fetch a chart once into the cache; returns the .tgz."""
    key = hashlib.sha256(repr(chart.identity).encode()).hexdigest()[:16]
    dest = cache / key
    existing = sorted(dest.glob("*.tgz")) if dest.exists() else []
    if existing:
        return existing[0]
    dest.mkdir(parents=True, exist_ok=True)
    cmd = ["helm", "pull", chart.ref, "-d", str(dest)]
    if chart.repo:
        cmd += ["--repo", chart.repo]
    if chart.version:
        cmd += ["--version", chart.version]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"helm pull {chart.label}: {result.stderr.strip()}")
    return sorted(dest.glob("*.tgz"))[0]


def chart_ids(tgz: Path) -> set:
    """`<name>-<version>` of the chart and every subchart in the archive."""
    ids = set()
    with tarfile.open(tgz) as tar:
        for member in tar.getmembers():
            if member.name.endswith("/Chart.yaml"):
                c = yaml.safe_load(tar.extractfile(member)) or {}
                ids.add(f"{c.get('name')}-{c.get('version')}")
    return ids


def render(rel: Release, chart: Chart, cache: Path, workdir: Path, kube_version: str) -> tuple[str, set]:
    """helm template; returns the manifest and the chart's ids."""
    tgz = pull(chart, cache)
    values_file = workdir / f"values-{hashlib.sha256(tgz.name.encode()).hexdigest()[:8]}.yaml"
    values_file.write_text(dump(rel.values))
    result = subprocess.run(
        [
            "helm", "template", rel.release_name, str(tgz),
            "--namespace", rel.target_namespace,
            "--values", str(values_file),
            "--include-crds", "--skip-tests",
            "--kube-version", kube_version,
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"helm template {chart.label}: {result.stderr.strip()}")
    return redact_secrets(result.stdout), chart_ids(tgz)


# Valid base64 (8 characters of its alphabet), so a redacted Secret's `data`
# is still a valid manifest, and readable in a diff.
REDACTED = "REDACTED"


def redact_secrets(manifest: str) -> str:
    """The manifest with every Secret data/stringData value replaced by
    REDACTED, before anything writes or posts it: renders go into PR comments
    and workflow artifacts on a public repo. Charts that fill an unset password
    with randAlphaNum (grafana's admin-password) would otherwise publish it,
    and show it as changed on every render. Other documents are untouched."""
    parts = re.split(r"(^---\s*$)", manifest, flags=re.M)
    for i, raw in enumerate(parts):
        if not re.search(r"^kind:\s*Secret\s*$", raw, flags=re.M):
            continue
        obj = yaml.load(raw, Loader=Loader)
        for key in ("data", "stringData"):
            if isinstance(obj.get(key), dict):
                obj[key] = {k: REDACTED for k in obj[key]}
        comments = "".join(line for line in raw.splitlines(keepends=True) if line.startswith("#"))
        parts[i] = f"\n{comments}{dump(obj)}"
    return "".join(parts)


@dataclass
class Rendered:
    objects: dict   # (kind, ns, name) -> obj, CRDs excluded
    crds: dict      # name -> (obj, from crds/ dir)
    chart_ids: set  # `<chart>-<version>` of the chart and its subcharts


def split(text: str, namespace: str, chart_ids: set) -> Rendered:
    """Objects keyed by (kind, namespace, name); an object in the release's
    namespace, set explicitly or left to Helm, is keyed with no namespace."""
    objects, crds = {}, {}
    for raw in re.split(r"^---\s*$", text, flags=re.M):
        source = re.search(r"^# Source: (\S+)", raw, flags=re.M)
        try:
            docs = load_all(raw)
        except yaml.YAMLError:
            continue
        for obj in docs:
            if obj.get("kind") == "CustomResourceDefinition":
                from_crds_dir = bool(source and "/crds/" in f"/{source.group(1)}")
                crds[meta(obj).get("name")] = (obj, from_crds_dir)
            else:
                ns = meta(obj).get("namespace", "")
                key = (obj.get("kind"), "" if ns == namespace else ns, meta(obj).get("name"))
                objects[key] = obj
    return Rendered(objects, crds, chart_ids)

