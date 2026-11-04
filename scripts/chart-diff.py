#!/usr/bin/env python3
"""Render the Helm charts a change bumps, and compare them.

Usage:
  chart-diff.py --base <checkout> [--head <checkout>] --out <dir> --kube-version <v>

For every HelmRelease whose chart (HelmRepository chart + version, or
OCIRepository url + ref) differs between the two checkouts, this:

  1. builds each Flux Kustomization with kustomize, applies the postBuild
     substitutions Flux would (cluster-settings and the Kustomization's own
     `substitute`; variables that only the cluster has, such as
     cluster-config-secret's, become PLACEHOLDER_<NAME>),
  2. resolves the HelmRelease's values (valuesFrom ConfigMaps found in the
     repo, then `values`),
  3. runs `helm template` for the old and new chart with the same values,
  4. diffs the rendered objects, summarises CRD changes, and runs
     kubeconform -strict on the new render.

It writes <out>/comment.md (the "Chart Diff" PR comment) and, per release,
<out>/<namespace>-<name>/{old,new}.yaml, diff.patch and kubeconform.txt.
Exits 1 if a render or kubeconform fails, 0 otherwise.

Requires: kustomize, helm, kubeconform, PyYAML.
"""

import argparse
import difflib
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fluxrender import Release, Rendered, dump, load_tree, releases, render, split

COMMENT_LIMIT = 60000
CRD_CATALOG_URL = (
    "https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/"
    "{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json"
)

# Labels every chart bumps on every object; left in, they'd make every object
# look changed. Besides these, any label whose value is `<chart>-<version>`
# (kube-prometheus-stack's `chart`, for one) is dropped.
VERSION_LABELS = ("helm.sh/chart", "app.kubernetes.io/version")


def without_version_labels(obj, chart_ids: set):
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "labels" and isinstance(v, dict):
                v = {
                    lk: lv for lk, lv in v.items()
                    if not (isinstance(lv, str) and (lk in VERSION_LABELS or lv in chart_ids))
                }
            out[k] = without_version_labels(v, chart_ids)
        return out
    if isinstance(obj, list):
        return [without_version_labels(v, chart_ids) for v in obj]
    return obj


def redacted(obj: dict) -> dict:
    """A Secret with every data/stringData value masked, so the PR comment
    shows keys added or removed but never a value. This also hides the churn
    from charts that fill an unset password with randAlphaNum, which differs
    on every `helm template`."""
    if obj.get("kind") != "Secret":
        return obj
    out = dict(obj)
    for key in ("data", "stringData"):
        if isinstance(out.get(key), dict):
            out[key] = {k: "<redacted>" for k in out[key]}
    return out


def clip(line: str) -> str:
    """Long single-line values (base64 blobs, bundled CRDs) say nothing in a diff."""
    if len(line) <= 300:
        return line
    return f"{line[:200]}... ({len(line)} characters)\n"


def object_diff(old: Rendered, new: Rendered) -> tuple[str, dict]:
    """One unified diff per added, removed or changed object, in a stable order."""
    ids = old.chart_ids | new.chart_ids
    counts = {"added": 0, "removed": 0, "changed": 0}
    parts = []
    for key in sorted(old.objects.keys() | new.objects.keys(), key=lambda k: tuple(str(p) for p in k)):
        o = redacted(without_version_labels(old.objects[key], ids)) if key in old.objects else None
        n = redacted(without_version_labels(new.objects[key], ids)) if key in new.objects else None
        if o == n:
            continue
        counts["added" if o is None else "removed" if n is None else "changed"] += 1
        kind, ns, name = key
        label = f"{kind} {ns}/{name}" if ns else f"{kind} {name}"
        parts.append("".join(clip(line) for line in difflib.unified_diff(
            dump(o).splitlines(keepends=True) if o is not None else [],
            dump(n).splitlines(keepends=True) if n is not None else [],
            f"a: {label}" if o is not None else "/dev/null",
            f"b: {label}" if n is not None else "/dev/null",
            n=3,
        )))
    return "".join(parts), counts


# ---------------------------------------------------------------------------
# CRD diff
# ---------------------------------------------------------------------------

def schema_fields(schema: dict, prefix: str = "") -> dict:
    """Flatten an openAPIV3Schema into {path: type}."""
    out = {}
    for name, sub in (schema.get("properties") or {}).items():
        path = f"{prefix}.{name}" if prefix else name
        out[path] = sub.get("type", "")
        out.update(schema_fields(sub, path))
    items = schema.get("items")
    if isinstance(items, dict):
        out.update(schema_fields(items, f"{prefix}[]"))
    return out


def versions(crd: dict) -> dict:
    return {v["name"]: v for v in (crd.get("spec") or {}).get("versions") or []}


def storage_version(crd: dict) -> str | None:
    for name, v in versions(crd).items():
        if v.get("storage"):
            return name
    return None


def crd_diff(old: Rendered, new: Rendered) -> list[str]:
    lines = []
    for name in sorted(new.crds.keys() - old.crds.keys()):
        lines.append(f"- **added** `{name}`")
    for name in sorted(old.crds.keys() - new.crds.keys()):
        lines.append(f"- **removed** `{name}`: existing objects of this kind are orphaned or deleted")
    for name in sorted(old.crds.keys() & new.crds.keys()):
        ids = old.chart_ids | new.chart_ids
        o = without_version_labels(old.crds[name][0], ids)
        n = without_version_labels(new.crds[name][0], ids)
        if o == n:
            continue
        if o.get("spec") == n.get("spec"):
            lines.append(f"- `{name}`: labels or annotations only")
            continue
        ov, nv = versions(o), versions(n)
        found = []
        for v in sorted(ov.keys() - nv.keys()):
            found.append(
                f"version `{v}` dropped: the API server refuses the new CRD while "
                f"`status.storedVersions` still lists it"
            )
        for v in sorted(ov.keys() & nv.keys()):
            if ov[v].get("served", True) and not nv[v].get("served", True):
                found.append(f"version `{v}` no longer served")
        for v in sorted(nv.keys() - ov.keys()):
            found.append(f"version `{v}` added")
        os_, ns_ = storage_version(o), storage_version(n)
        if os_ != ns_:
            found.append(
                f"storage version `{os_}` → `{ns_}`: new writes are stored as `{ns_}` and "
                f"`status.storedVersions` gains it; objects stay in `{os_}` until rewritten"
            )
        for v in sorted(ov.keys() & nv.keys()):
            of = schema_fields(((ov[v].get("schema") or {}).get("openAPIV3Schema")) or {})
            nf = schema_fields(((nv[v].get("schema") or {}).get("openAPIV3Schema")) or {})
            removed = sorted(of.keys() - nf.keys())
            retyped = sorted(k for k in of.keys() & nf.keys() if of[k] != nf[k])
            if removed:
                shown = ", ".join(f"`{f}`" for f in removed[:20])
                more = f" and {len(removed) - 20} more" if len(removed) > 20 else ""
                found.append(f"`{v}` fields removed: {shown}{more}")
            if retyped:
                shown = ", ".join(f"`{f}` ({of[f] or 'any'} → {nf[f] or 'any'})" for f in retyped[:20])
                found.append(f"`{v}` fields changed type: {shown}")
        added = sum(
            len(set(schema_fields(((nv[v].get("schema") or {}).get("openAPIV3Schema")) or {}))
                - set(schema_fields(((ov[v].get("schema") or {}).get("openAPIV3Schema")) or {})))
            for v in ov.keys() & nv.keys()
        )
        if added:
            found.append(f"{added} field(s) added")
        if not found:
            found.append("schema changed (descriptions, validation or defaults); no versions or fields removed")
        lines.append(f"- `{name}`")
        lines.extend(f"  - {f}" for f in found)
    return lines


# ---------------------------------------------------------------------------
# kubeconform
# ---------------------------------------------------------------------------

def close(node) -> None:
    """Deny unknown fields on every object schema that lists its fields, as
    the API server does for a structural CRD; preserve-unknown subtrees stay
    open."""
    if isinstance(node, dict):
        if (
            node.get("type") == "object"
            and "properties" in node
            and "additionalProperties" not in node
            and not node.get("x-kubernetes-preserve-unknown-fields")
        ):
            node["additionalProperties"] = False
        if node.get("x-kubernetes-int-or-string"):
            node.pop("type", None)
            node["anyOf"] = [{"type": "integer"}, {"type": "string"}]
        for v in node.values():
            close(v)
    elif isinstance(node, list):
        for v in node:
            close(v)


def write_crd_schemas(crds: list[dict], dest: Path) -> None:
    """JSON schemas for kubeconform from the CRDs the new charts ship, so
    custom resources are checked against the version they'll meet."""
    for crd in crds:
        spec = crd.get("spec") or {}
        kind = (spec.get("names") or {}).get("kind", "")
        for v in spec.get("versions") or []:
            schema = (v.get("schema") or {}).get("openAPIV3Schema")
            if not schema:
                continue
            props = schema.setdefault("properties", {})
            props.setdefault("apiVersion", {"type": "string"})
            props.setdefault("kind", {"type": "string"})
            props.setdefault("metadata", {"type": "object"})
            close(schema)
            path = dest / spec["group"] / f"{kind.lower()}_{v['name']}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(schema))


def kubeconform(manifest: Path, schemas: Path) -> tuple[bool, str]:
    result = subprocess.run(
        [
            "kubeconform", "-strict", "-summary", "-ignore-missing-schemas",
            "-schema-location", "default",
            "-schema-location", f"{schemas}/{{{{.Group}}}}/{{{{.ResourceKind}}}}_{{{{.ResourceAPIVersion}}}}.json",
            "-schema-location", CRD_CATALOG_URL,
            str(manifest),
        ],
        capture_output=True, text=True,
    )
    report = (result.stdout + result.stderr).replace(f"{manifest} - ", "").strip()
    return result.returncode == 0, report


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

@dataclass
class Section:
    key: tuple
    title: str
    versions: str
    body: list
    diff: str = ""
    counts: dict | None = None
    crds_changed: bool = False
    manifest: Path | None = None
    old_crds: list = field(default_factory=list)
    crds: list = field(default_factory=list)
    conform: bool | None = None
    conformed_before: bool | None = None

    @property
    def failed(self) -> bool:
        """A render that fails, or a kubeconform failure this change brings.
        One the old render has too is reported but doesn't fail the check."""
        return self.counts is None or (self.conform is False and self.conformed_before is True)

    @property
    def summary_row(self) -> str:
        name = f"`{self.key[0]}/{self.key[1]}`"
        if self.counts is None:
            return f"| {name} | {self.versions} | render failed | | |"
        c = self.counts
        objs = f"+{c['added']} −{c['removed']} ~{c['changed']}"
        crds = "changed" if self.crds_changed else "none"
        conform = "pass" if self.conform else "fail" if self.conformed_before else "fail (already failing)"
        return f"| {name} | {self.versions} | {objs} | {crds} | {conform} |"


def review(key, old: Release, new: Release, cache: Path, out: Path, kube_version: str) -> Section:
    ns, name = key
    rdir = out / f"{ns}-{name}"
    rdir.mkdir(parents=True, exist_ok=True)
    section = Section(
        key,
        f"`{ns}/{name}`: {old.chart.label} → {new.chart.label}",
        f"{old.chart.version or '-'} → {new.chart.version or '-'}",
        [f"- {n}" for n in dict.fromkeys(old.notes + new.notes)],
    )
    try:
        old_text, old_ids = render(old, old.chart, cache, rdir, kube_version)
        new_text, new_ids = render(new, new.chart, cache, rdir, kube_version)
    except RuntimeError as e:
        section.body.append(f"\n:x: Render failed:\n\n```\n{str(e)[-2000:]}\n```")
        return section
    (rdir / "old.yaml").write_text(old_text)
    (rdir / "new.yaml").write_text(new_text)
    section.manifest = rdir / "new.yaml"

    old_r = split(old_text, old.target_namespace, old_ids)
    new_r = split(new_text, new.target_namespace, new_ids)
    section.old_crds = [c[0] for c in old_r.crds.values()]
    section.crds = [c[0] for c in new_r.crds.values()]
    section.diff, section.counts = object_diff(old_r, new_r)
    (rdir / "diff.patch").write_text(section.diff)

    crd_lines = crd_diff(old_r, new_r)
    if crd_lines:
        section.crds_changed = True
        section.body.append("\n**CRDs**\n")
        section.body.extend(crd_lines)
        if any(c[1] for c in new_r.crds.values()) and new.crds_on_upgrade == "Skip":
            section.body.append(
                "\n:warning: This HelmRelease's `upgrade.crds` is `Skip` (Flux's default), so "
                "CRDs from the chart's `crds/` directory are not updated by the upgrade."
            )
    return section


def conform(section: Section, schemas: Path, old_schemas: Path) -> None:
    """kubeconform the new render; on failure, the old one too, to tell a
    new problem from one the cluster already has."""
    ok, report = kubeconform(section.manifest, schemas)
    section.conform = ok
    (section.manifest.parent / "kubeconform.txt").write_text(report)
    if ok:
        return
    section.conformed_before, _ = kubeconform(section.manifest.parent / "old.yaml", old_schemas)
    when = "new with this change" if section.conformed_before else "the old render fails too"
    section.body.append(
        f"\n:x: kubeconform -strict on the new render ({when}):\n\n```\n{report[-3000:]}\n```"
    )


def comment(sections: list[Section], build_errors: list[str]) -> str:
    parts = ["### Chart Diff", ""]
    if not sections:
        parts.append("No HelmRelease chart versions change. :white_check_mark:")
        return "\n".join(parts) + "\n"
    parts += [
        "Rendered with `helm template` and each HelmRelease's values from this repo. "
        "Objects: added, removed, changed, ignoring the `helm.sh/chart` and "
        "`app.kubernetes.io/version` labels; CRDs are summarised separately.",
        "",
        "| HelmRelease | Chart | Objects | CRDs | kubeconform |",
        "|---|---|---|---|---|",
    ]
    parts += [s.summary_row for s in sections]
    if build_errors:
        parts += ["", "<details><summary>kustomize build errors (those paths were skipped)</summary>", ""]
        parts += [f"- `{e}`" for e in build_errors]
        parts += ["", "</details>"]
    head = "\n".join(parts) + "\n"
    texts = []
    for s in sections:
        body = "\n".join(s.body).strip("\n")
        texts.append(f"\n#### {s.title}\n" + (f"\n{body}\n" if body else ""))
    budget = COMMENT_LIMIT - len(head) - sum(len(t) for t in texts) - 200 * len(sections)
    per_diff = max(budget // max(len(sections), 1), 0)
    for s, t in zip(sections, texts):
        head += t
        if s.diff:
            d = s.diff
            if len(d) > per_diff:
                d = d[:per_diff] + "\n... (truncated; the full diff is in the workflow artifacts)\n"
            n = s.diff.count("\n")
            head += (
                f"\n<details><summary>Rendered diff ({n} lines)</summary>\n\n"
                f"```diff\n{d}\n```\n\n</details>\n"
            )
        elif s.counts is not None:
            head += "\nNo rendered changes outside CRDs.\n"
    if len(head) > COMMENT_LIMIT:
        head = head[:COMMENT_LIMIT] + "\n\n... (truncated; the full output is in the workflow artifacts)\n"
    return head


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base", type=Path, required=True, help="checkout of the base branch")
    parser.add_argument("--head", type=Path, default=Path("."), help="checkout of the change (default: .)")
    parser.add_argument("--out", type=Path, required=True, help="where to write comment.md and renders")
    parser.add_argument("--cache", type=Path, help="chart cache (default: <out>/charts)")
    parser.add_argument("--kube-version", required=True, help="the cluster's Kubernetes version, for Capabilities")
    args = parser.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    cache = args.cache or args.out / "charts"
    base, head = load_tree(args.base.resolve()), load_tree(args.head.resolve())
    old, new = releases(base), releases(head)

    sections = []
    for key in sorted(old.keys() & new.keys()):
        o, n = old[key], new[key]
        if o.chart is None or n.chart is None or o.chart.identity == n.chart.identity:
            continue
        print(f"==> {key[0]}/{key[1]}: {o.chart.label} -> {n.chart.label}", file=sys.stderr)
        sections.append(review(key, o, n, cache, args.out, args.kube_version))

    schemas, old_schemas = args.out / "crd-schemas" / "new", args.out / "crd-schemas" / "old"
    write_crd_schemas([c for s in sections for c in s.crds], schemas)
    write_crd_schemas([c for s in sections for c in s.old_crds], old_schemas)
    for s in sections:
        if s.manifest:
            conform(s, schemas, old_schemas)

    (args.out / "comment.md").write_text(comment(sections, sorted(set(head.build_errors))))
    print(f"{len(sections)} chart change(s); comment in {args.out / 'comment.md'}", file=sys.stderr)
    return 1 if any(s.failed for s in sections) else 0


if __name__ == "__main__":
    sys.exit(main())
