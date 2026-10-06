# Break-glass: reaching the cluster when the gateway or Authelia is down

For when `*.internal.<domain>` stops answering: Envoy is down, Authelia is crashlooping,
MetalLB has lost the VIP, or a bad HTTPRoute change has broken routing.

## Step 1: a kubeconfig that doesn't depend on Authelia

The everyday `homelab` kube context logs in through Authelia (`kubelogin get-token
--oidc-issuer-url=https://auth.internal.<domain>`). It keeps working only until its cached
token needs a refresh, so during an Authelia outage it will stop working too.

Use the **operator kubeconfig stored in 1Password** instead. It doesn't log in through OIDC,
so an Authelia or gateway outage doesn't affect it.

```bash
# Save the operator kubeconfig from 1Password to a file outside any repo, then:
export KUBECONFIG=~/operator.kubeconfig
kubectl get nodes                                 # confirm it works
```

Delete the file and `unset KUBECONFIG` when you're done.

Everything below works with either kubeconfig, as long as the apiserver is reachable.

## kube-web-view — the primary debugging tool

```bash
kubectl port-forward -n cluster-util svc/kube-web-view 8080:80
```

Then open <http://localhost:8080>.

Port-forward goes through the apiserver, bypassing Envoy Gateway, MetalLB, Authelia,
cert-manager and cluster DNS. kube-web-view reads all of its data from the apiserver, so
whenever port-forward works, kube-web-view works.

## Other services

Same pattern, different Service:

```bash
kubectl port-forward -n observability svc/prometheus-operated 9090:9090   # Prometheus
kubectl port-forward -n observability svc/loki-gateway        3100:80     # Loki
kubectl port-forward -n observability svc/grafana             3000:80     # Grafana
kubectl port-forward -n automation   svc/zigbee2mqtt          8081:8080   # Zigbee2MQTT
kubectl port-forward -n automation   svc/frigate              5000:5000   # Frigate
kubectl port-forward -n security     svc/authelia             9091:80     # Authelia itself
kubectl port-forward -n rook-ceph    svc/rook-ceph-mgr-dashboard 7000:7000
```

Confirm a Service name before relying on it, since Helm-generated names drift across chart
upgrades:

```bash
kubectl get svc -n <namespace>
```

Grafana over port-forward still requires an Authelia login, since it uses OIDC and
`oauth_auto_login: true` will redirect. If Authelia is the thing that's broken, use
Prometheus or Loki directly instead.

## When the apiserver itself is unreachable

Port-forward is gone at that point. Drop to Talos:

```bash
talosctl -n k8s-node-1 health
talosctl -n k8s-node-1 dmesg
talosctl -n k8s-node-1 services
talosctl -n k8s-node-1 containers -k          # kubelet's view of static pods
talosctl -n k8s-node-1 logs -k kube-apiserver
```

Node config and the `config-gen.sh` workflow are documented in `talos/README.md`.

## Quick triage for a gateway outage

```bash
kubectl get pods -n network                       # Envoy Gateway controller + proxies
kubectl get svc  -n network                       # LB IPs actually assigned?
kubectl get gateway,httproute,tlsroute -A         # Programmed / Accepted conditions
kubectl get securitypolicy -A                     # ext-authz policies Accepted?
kubectl get pods,certificate -n security          # Authelia up, certs valid?
kubectl logs -n security deploy/authelia --tail=100
```

Every `SecurityPolicy` sets `failOpen: false`, so when Authelia is down the protected
hostnames refuse requests instead of falling through to their unauthenticated backends.
If every Authelia-protected hostname fails at once while the others still load, look at
Authelia before Envoy.
