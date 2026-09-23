# friends-dns

Ad-blocking DNS for friends, served over a **separate Tailscale tailnet**.

- Workload: `kubernetes/apps/network/friends-dns/` — blocky StatefulSet, 2 replicas (`friends-dns-0`, `friends-dns-1`), each with a Tailscale sidecar (containerboot, kernel mode).
- Friends reach blocky on port 53 at each node's `100.x` Tailscale IP. Nothing else is exposed: no exit node, no subnet routes.
- Tailscale state lives in Secrets `friends-dns-0-tailscale` / `friends-dns-1-tailscale` (namespace `network`), so node identity and IPs survive pod restarts (`TS_AUTH_ONCE=true`).

**Order matters:** do steps 1–3 before merging. The StatefulSet starts pods in order, so `friends-dns-1` won't start until `friends-dns-0` is logged in to the tailnet, and that needs the 1Password item to exist.

> Tailscale docs moved from `tailscale.com/kb/...` to `tailscale.com/docs/...` in 2025–2026. If a UI label below doesn't match what you see, **verify in the admin console**.

---

## 1. Admin: create the friends' tailnet

1. Sign out of Tailscale in a private browser window, or use a separate browser profile.
2. Go to <https://login.tailscale.com/start> and sign up with an identity that is **not** the one your main tailnet uses, e.g. a separate Google or GitHub account made only for this. A new identity gets a new tailnet, so friends never join your main tailnet.
3. Pick the free **Personal** plan. Since the April 2026 pricing change, Personal is free for up to **6 users**, and Personal Plus no longer exists.
   - The 6 users include you (the admin identity), which leaves room for 5 friends.
   - A 7th user moves the **whole tailnet** onto a paid plan. Check the current terms on the pricing page before you invite more people.
4. From now on, do all admin work in this guide in the **friends'** tailnet. Check the tailnet name in the top-left of the admin console first.

## 2. Admin: access policy

Go to **Access controls** in the admin console and replace the entire policy file with the one below. It removes the default allow-all rule, which lets every device reach every other device. Access is denied unless a grant allows it, so the result is:

- Members can reach `tag:friends-dns` on 53/udp and 53/tcp, and nothing else.
- Friends can't reach each other's devices.
- The DNS nodes can't start connections to anyone. Replies to DNS queries still work.
- Blocky's HTTP API (port 4000, unauthenticated, able to switch blocking off) also listens on the tailnet IP. This policy is the only thing keeping friends off it, so don't add a broader grant for `tag:friends-dns`.

```jsonc
{
  // Only admins can apply tag:friends-dns. The OAuth client in step 3 is
  // scoped to this tag, which lets it mint auth keys carrying it.
  "tagOwners": {
    "tag:friends-dns": ["autogroup:admin"],
  },

  // DNS to the blocky nodes is the only thing allowed.
  // No other grants, so there's no member<->member or tag->member access.
  "grants": [
    {
      "src": ["autogroup:member"],
      "dst": ["tag:friends-dns"],
      "ip":  ["udp:53", "tcp:53"],
    },
  ],

  // Tailscale rejects any policy change that breaks these assertions.
  // Test src must be a real user: use your own admin login for this tailnet.
  "tests": [
    {
      "src":    "you@example.com",
      "accept": ["tag:friends-dns:53"],
      // 4000 = blocky's HTTP API
      "deny":   ["tag:friends-dns:4000", "tag:friends-dns:22"],
    },
    {
      "src":    "you@example.com",
      "proto":  "udp",
      "accept": ["tag:friends-dns:53"],
    },
  ],
}
```

Notes:
- `ip` port syntax (`udp:53`, `tcp:53`) and `autogroup:member` as `src` follow the grants examples in the docs.
- When `proto` is omitted, a test passes on either TCP or UDP. The second test pins UDP.
- Once friends join you can add a test with a friend's login as `src` and another friend's device IP in `deny`, to prove they can't reach each other.
- Leave `ssh` out. No `ssh` section means no Tailscale SSH.

## 3. Admin: OAuth client + 1Password

The sidecars authenticate with an OAuth client secret used as an auth key. The ExternalSecret renders it as `TS_AUTHKEY=<secret>?ephemeral=false&preauthorized=true`.

1. In the admin console, open **Settings → Trust credentials**. This page replaced "OAuth clients" on 2025-10-30.
2. Click **Credential → OAuth**.
3. Scopes: **Keys → Auth Keys: Write**. Nothing else.
4. Tags: add `tag:friends-dns`. This only works after the step 2 policy is saved, because the tag must exist in `tagOwners`.
5. Click **Generate credential**. Copy the **client secret** now, because it is shown only once. You don't need the client ID.
6. In 1Password, open the vault that the `onepassword-connect` ClusterSecretStore reads and create:
   - Item: `friends-dns-tailscale`
   - Field: `CLIENT_SECRET` = the secret from step 5
7. To revoke it later, go to **Trust credentials**, find the credential, and click **Revoke**. Nodes that are already registered keep working because their state is in the k8s Secrets. New registrations fail until you store a fresh secret.

## 4. Admin: deploy and verify

1. Merge the PR.
2. Reconcile:
   ```sh
   flux reconcile ks friends-dns -n flux-system --with-source
   kubectl -n network get pods -l app.kubernetes.io/name=friends-dns
   ```
   Both `friends-dns-0` and `friends-dns-1` should be `Running` with `2/2` containers ready. The tailscale container only reports ready once it has a tailnet IP.
3. In the friends' admin console, open **Machines**. `friends-dns-0` and `friends-dns-1` should both appear, tagged `tag:friends-dns`, with no owner shown.
4. Get their IPs:
   ```sh
   kubectl -n network exec friends-dns-0 -c tailscale -- tailscale ip -4
   kubectl -n network exec friends-dns-1 -c tailscale -- tailscale ip -4
   ```
5. From a device logged in to the **friends'** tailnet (a spare phone or laptop signed in as you), run:
   ```sh
   dig @100.x.y.z doubleclick.net +short   # expect 0.0.0.0
   dig @100.x.y.z example.com +short       # expect a real IP
   ```
   Test both IPs.

If a pod has no tailnet IP, check `kubectl -n network logs friends-dns-0 -c tailscale`. The usual causes are a wrong or revoked secret, a missing `auth_keys` scope, or a tag missing from the OAuth client or from `tagOwners`.

## 5. Admin: DNS settings

On the **DNS** page of the admin console:

1. Enable **MagicDNS**. Blocky uses it for reverse lookups via `100.100.100.100`, which is how metrics show device names instead of IPs.
2. Under **Global nameservers**, click **Add nameserver → Custom** and add both `100.x` IPs from step 4. Two entries give redundancy if one pod is restarting.
3. Enable **Override DNS servers** (under Global nameservers). Devices then ignore their local DNS and use only these nameservers. Without it, most devices keep their own DNS and nothing gets filtered.
4. Key expiry on the DNS nodes: tagged devices have key expiry **disabled by default** when they first authenticate with a tag. On **Machines**, confirm that both nodes show expiry disabled. If one doesn't, open its **⋯ menu → Disable key expiry**.

## 6. Adding a friend

1. Open **Users** in the admin console and click **Invite external users**. Choose one:
   - **Email** the invite, or
   - **Copy invite link**: pick the **Member** role, then **Generate & copy invite link**, and send the link yourself.
2. Optional hardening under **Settings → Device management** (verify labels):
   - **User approval**: invited users are auto-approved, so this only stops uninvited sign-ups.
   - **Device approval**: you approve each new device before it can connect. More work, but it stops friends from adding lots of devices.
3. User device key expiry: the default is **180 days**. After that the friend must sign in again, and DNS stops working until they do. To avoid this, open **Machines**, find their device, and use **⋯ → Disable key expiry**. Alternatively, change the tailnet-wide expiry period under **Settings → Device management → Key expiry** (verify location).
4. Remember the 6-user cap (step 1).

**Removing a friend:** open **Users**, find the user, and use **⋯ → Remove user**. Their devices leave the tailnet and free up the seat. Removing one device is also possible from **Machines → ⋯ → Remove**.

## 7. Friend-facing instructions (copy and send)

```text
Ad-blocking DNS: setup

What this is: I run an ad/tracker blocker at home. You connect to it with
the free Tailscale app. It ONLY handles DNS (looking up website names).
Your actual browsing does NOT go through my server, and I can't see what
you do on websites.

Privacy, honestly: every website NAME your device looks up is sent to my
server while Tailscale is on. I don't keep a log of those lookups, but the
server keeps aggregate stats (query counts, blocked counts, per-device
totals) that I can see.

Setup
1. Install Tailscale:
   - iPhone/iPad/Mac: App Store, "Tailscale"
   - Android: Play Store, "Tailscale"
   - Windows: https://tailscale.com/download
2. Open the invite link I sent you and sign in (Google, Apple, Microsoft,
   GitHub, etc. Any account is fine and it doesn't need to match mine).
3. Open the Tailscale app, sign in with the SAME account, and turn it on.
   Allow the VPN / network extension when your device asks.
4. Done. Ads should now be blocked in apps and browsers.

If a website or app breaks
- Quickest fix: turn Tailscale OFF.
  - iPhone: Settings > VPN, or the Tailscale app toggle
  - Android: Quick Settings tile, or the Tailscale app toggle
  - Mac: Tailscale menu-bar icon > disconnect
  - Windows: Tailscale tray icon > Disconnect
- Or keep Tailscale on and just stop using my DNS:
  - Mac: Tailscale menu-bar icon > Preferences (Settings) > untick
    "Use Tailscale DNS settings"
  - Windows: hold SHIFT and right-click the Tailscale tray icon > untick
    "Use Tailscale DNS settings"
  - iPhone / Android: in the Tailscale app, open Settings and turn off
    "Use Tailscale DNS settings"
- Then tell me which site or app broke so I can unblock it for everyone.
  Turn it back on afterwards.

If NOTHING loads while Tailscale is on, my server (or my home internet)
is probably down. Turn Tailscale off and tell me.
```

(The iOS/Android location of "Use Tailscale DNS settings" is not confirmed in the docs. Check it in the current app before sending.)

## 8. Maintenance

**Allowlisting a broken domain**
1. Find the domain. Look at blocky's blocked-query metrics, or ask the friend what broke and test it with `dig @100.x.y.z <domain>`.
2. Add it to `allowlists.ads` in `kubernetes/apps/network/friends-dns/app/configs/config.yaml`.
3. Commit and push. Flux applies the new ConfigMap, and reloader restarts the pods **one at a time**. With both IPs set as global nameservers, friends shouldn't notice.
4. Check with `dig @100.x.y.z <domain>`, which should now return a real IP.

**Metrics**
- Blocky exposes Prometheus metrics: query counts, blocked counts, cache hits, and per-client counts. MagicDNS reverse lookups label clients with device names. Look at them in Grafana, or `kubectl -n network port-forward svc/friends-dns 4000` and read `http://localhost:4000/metrics`.
- Query logging is **off**. Keep it that way, because that's what the friend-facing privacy note says.

**Home internet outage / cluster down**
- Global nameservers with Override DNS means friends' devices send all DNS to the two `100.x` nodes. If both are unreachable (home internet down, both pods down, or the cluster down), **name resolution fails for friends while Tailscale is on**, and websites won't load.
- The fix on their side is to turn Tailscale off. Nothing needs to change when service comes back.
- One pod down on its own is fine, because clients use the other nameserver.
- Tailscale identities persist in the `friends-dns-*-tailscale` Secrets. If you lose or delete them, the nodes register again with **new** 100.x IPs, and you must update the Global nameservers in step 5. If that happens, remove the stale machines from **Machines** too.

**Rotating the OAuth secret:** generate a new credential (step 3), update 1Password, and revoke the old one. Running nodes don't need it (TS_AUTH_ONCE plus stored state). The new secret only matters for re-registration.
