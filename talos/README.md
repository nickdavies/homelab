## Booting / Init

First boot the machine into mantenance mode (For details see the [`image_build` README](image_build/README.md)) then.

generate a new config:
```
./config-gen.sh k8s-node-1
```

This will create one of the files (depending on the value of `NODE_TYPE` in it's environment):
```
output/rendered/nodes/k8s-node-1/controlplane.yaml
output/rendered/nodes/k8s-node-1/worker.yaml
```

which you can then apply with:
```
talosctl apply -f output/rendered/nodes/k8s-node-1/controlplane.yaml -n k8s-node-1 --insecure
```
