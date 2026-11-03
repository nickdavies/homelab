#!/usr/bin/env python3
"""Check that the cluster can boot from empty: every custom resource's CRD
is guaranteed to exist before Flux applies the resource.

Usage:
  crd-order.py --kube-version <v> [--cache <dir>]

Builds every Flux Kustomization and renders every HelmRelease (fluxrender),
then models what Flux guarantees has happened before each one acts:

  - a Kustomization is applied only after the Kustomization that applies it,
    and after each of its dependsOn is Ready;
  - a Kustomization is Ready once applied, and with `wait: true` only once
    every HelmRelease and Kustomization it applies is Ready too;
  - a HelmRelease installs only after its Kustomization is applied and each
    of its dependsOn is Ready.

A custom resource passes if some provider of its CRD comes earlier in that
order, or is the same Kustomization or HelmRelease (both apply CRDs first).
A CRD a Kustomization applies is provided once it's applied; one a
HelmRelease renders, once it's Ready. Talos bootstrap manifests don't count:
the point is that Flux alone brings the cluster up.

A Kustomization that only waits for a HelmRelease's Kustomization (with
`wait: false`) is not ordered after the HelmRelease's install, so a CRD from a
chart guarantees nothing to it.

Exits 1 on any custom resource whose CRD isn't guaranteed, any custom kind
with no CRD anywhere in the repo, or any HelmRelease that fails to render.

Requires: kustomize, helm, PyYAML.
"""

import argparse
import sys
import tempfile
from pathlib import Path

from fluxrender import Tree, load_all, load_tree, meta, releases, render

# API groups the API server serves itself.
BUILTIN_GROUPS = {
    "", "apps", "batch", "policy", "autoscaling",
    "admissionregistration.k8s.io", "apiextensions.k8s.io", "apiregistration.k8s.io",
    "certificates.k8s.io", "coordination.k8s.io", "discovery.k8s.io",
    "flowcontrol.apiserver.k8s.io", "networking.k8s.io", "node.k8s.io",
    "rbac.authorization.k8s.io", "resource.k8s.io", "scheduling.k8s.io", "storage.k8s.io",
}
# Groups whose CRDs exist before Flux applies anything: flux-operator's
# install.yaml and the controllers the FluxInstance installs.
BOOTSTRAP_GROUPS = {
    "fluxcd.controlplane.io",
    "kustomize.toolkit.fluxcd.io", "helm.toolkit.fluxcd.io",
    "source.toolkit.fluxcd.io", "notification.toolkit.fluxcd.io",
}

# Groups whose CRDs come from outside Flux and stay there.
EXTERNAL_GROUPS = {
    # serviceaccounts.talos.dev: Talos installs it for
    # machine.features.kubernetesTalosAPIAccess.
    "talos.dev",
}

KS, HR = "Kustomization", "HelmRelease"


def group_kind(obj: dict) -> tuple[str, str]:
    api = str(obj.get("apiVersion", ""))
    return (api.split("/")[0] if "/" in api else "", obj.get("kind", ""))


def is_custom(gk: tuple[str, str]) -> bool:
    group, kind = gk
    if kind == "CustomResourceDefinition":
        return False
    return not (group in BUILTIN_GROUPS or group in BOOTSTRAP_GROUPS or group in EXTERNAL_GROUPS)


def is_flux_ks(obj: dict) -> bool:
    return group_kind(obj) == ("kustomize.toolkit.fluxcd.io", KS)


def is_hr(obj: dict) -> bool:
    return group_kind(obj) == ("helm.toolkit.fluxcd.io", HR)


def label(fact: tuple) -> str:
    state, kind, name = fact
    return f"{kind} {name}"


class Order:
    """Facts are ("applied" | "ready", kind, name); a HelmRelease's name is
    `<namespace>/<name>`. before() is everything guaranteed to have happened
    before a fact."""

    def __init__(self, tree: Tree):
        self.ks: dict = {}       # name -> spec
        self.parent: dict = {}   # Kustomization name -> the Kustomization that applies it
        self.hrs: dict = {}      # "<ns>/<name>" -> (spec, owning Kustomization)
        self.children: dict = {} # Kustomization name -> [facts it applies]
        for owner, objs in tree.applied.items():
            for obj in objs:
                if is_flux_ks(obj):
                    name = meta(obj)["name"]
                    self.ks[name] = obj.get("spec") or {}
                    self.parent[name] = owner
                    self.children.setdefault(owner, []).append(("ready", KS, name))
                elif is_hr(obj):
                    name = f"{meta(obj).get('namespace', '')}/{meta(obj)['name']}"
                    self.hrs[name] = (obj.get("spec") or {}, owner)
                    self.children.setdefault(owner, []).append(("ready", HR, name))
        self._memo: dict = {}

    def preconditions(self, fact: tuple) -> list:
        state, kind, name = fact
        if kind == KS and state == "applied":
            pre = [("ready", KS, d["name"]) for d in self.ks.get(name, {}).get("dependsOn") or []]
            if name in self.parent:
                pre.append(("applied", KS, self.parent[name]))
            return pre
        if kind == KS and state == "ready":
            pre = [("applied", KS, name)]
            if self.ks.get(name, {}).get("wait"):
                pre += self.children.get(name, [])
            return pre
        if kind == HR:
            spec, owner = self.hrs[name]
            ns = name.split("/")[0]
            return [("applied", KS, owner)] + [
                ("ready", HR, f"{d.get('namespace', ns)}/{d['name']}") for d in spec.get("dependsOn") or []
            ]
        return []

    def before(self, fact: tuple) -> set:
        """Transitive preconditions. A cycle contributes nothing past itself:
        Flux would never get there, and the check reports what's missing."""
        if fact in self._memo:
            return self._memo[fact]
        self._memo[fact] = set()
        out: set = set()
        for p in self.preconditions(fact):
            out.add(p)
            out |= self.before(p)
        self._memo[fact] = out
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--kube-version", required=True, help="the cluster's Kubernetes version, for Capabilities")
    parser.add_argument("--cache", type=Path, help="chart cache (default: a temporary directory)")
    args = parser.parse_args()

    tree = load_tree(Path(".").resolve())
    order = Order(tree)
    problems: list[str] = list(f"kustomize build failed: {e}" for e in tree.build_errors)

    # Every unit (a Kustomization's plain objects, or a HelmRelease's render)
    # with the fact at which it acts and the objects it creates.
    units = [(("applied", KS, k), objs) for k, objs in tree.applied.items()]
    with tempfile.TemporaryDirectory() as tmp:
        cache = args.cache or Path(tmp) / "charts"
        for (ns, name), rel in sorted(releases(tree).items()):
            fact = ("ready", HR, f"{ns}/{name}")
            if rel.chart is None:
                problems.append(f"{label(fact)}: not rendered ({'; '.join(rel.notes)})")
                continue
            print(f"==> rendering {ns}/{name}", file=sys.stderr)
            try:
                text, _ = render(rel, rel.chart, cache, Path(tmp), args.kube_version)
            except RuntimeError as e:
                problems.append(f"{label(fact)}: {str(e)[-500:]}")
                continue
            units.append((fact, load_all(text)))

    # Who provides each custom kind, and who uses it: a unit that succeeded
    # in applying a kind proves its CRD existed by then.
    providers: dict = {}
    users: dict = {}
    for fact, objs in units:
        for obj in objs:
            if obj.get("kind") == "CustomResourceDefinition":
                spec = obj.get("spec") or {}
                gk = (spec.get("group"), (spec.get("names") or {}).get("kind"))
                providers.setdefault(gk, set()).add(fact)
            elif is_custom(group_kind(obj)):
                users.setdefault(group_kind(obj), set()).add(fact)

    deadlocks, stalls, retries = [], [], []
    for fact, objs in units:
        # Ready of an earlier Kustomization implies it was applied.
        earlier = order.before(fact)
        earlier |= {("applied", KS, f[2]) for f in earlier if f[1] == KS}
        # What this unit succeeding means, to spot a provider waiting on it.
        done = {fact, ("ready", KS, fact[2])} if fact[1] == KS else {fact}
        for gk in sorted({group_kind(o) for o in objs if is_custom(group_kind(o))}):
            what = f"{label(fact)} applies {gk[1]}.{gk[0]}"
            if not providers.get(gk):
                problems.append(f"{what}, but no CRD for it is anywhere in the repo")
                continue
            # The unit itself applies its CRDs first.
            if providers[gk] & (earlier | {fact}) or users.get(gk, set()) & earlier:
                continue
            source = ", ".join(sorted(label(p) for p in providers[gk]))
            if all(done & (order.before(p) | {p}) for p in providers[gk]):
                deadlocks.append(f"{what}; its CRD comes from {source}, which waits for it")
            elif fact[1] == HR:
                stalls.append(f"{what}; its CRD comes from {source}, which isn't ordered before it")
            else:
                retries.append(f"{what}; its CRD comes from {source}, which isn't ordered before it")

    def report(title: str, items: list) -> None:
        if items:
            print(f"\n{title} ({len(items)}):\n")
            for p in sorted(items):
                print(f"  - {p}")

    report("Deadlocks: the CRD's provider can't come up until this does", deadlocks)
    report("HelmRelease installs that fail and stall once remediation retries run out", stalls)
    report("Other problems", problems)
    report("Kustomizations that fail until the CRD appears, then converge on retryInterval (not failing the check)",
           retries)
    failed = deadlocks + stalls + problems
    if failed:
        print(f"\nFAIL: the cluster can't boot from empty with Flux alone (Talos bootstrap manifests are ignored).")
        return 1
    print(f"\nOK: no deadlocks or stalling installs across {len(units)} Kustomizations and HelmReleases.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
