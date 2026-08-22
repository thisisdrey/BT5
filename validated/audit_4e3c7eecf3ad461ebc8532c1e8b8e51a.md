### Title
`handleHostUpdateNotification` omits `remote_allow_list` filtering applied by `handleHostPunchNotification`, allowing RemoteList poisoning - ([File: lighthouse.go])

### Summary
`LightHouseHandler.handleHostUpdateNotification` (lighthouse.go:1330-1407) writes attacker-supplied `V4AddrPorts`/`V6AddrPorts`/relay addresses directly into the lighthouse's `RemoteList` via `am.unlockedSetV4/unlockedSetV6/unlockedSetRelay` without ever calling `lhh.lh.GetRemoteAllowList()`/`remoteAllowList.Allow(...)`. By contrast, `handleHostPunchNotification` (lighthouse.go:1409-1452) explicitly gates every address through `remoteAllowList.Allow(detailsVpnAddr, b.Addr())` before acting on it. This inconsistency lets a peer report underlay addresses that `lighthouse.remote_allow_list` is configured to reject, and have them accepted into the lighthouse's cached `RemoteList` anyway.

### Finding Description
When a lighthouse receives a `HostUpdateNotification`, it derives `fromVpnAddrs` from the packet's authenticated source VPN address and validates only that `detailsVpnAddr` (if present) matches `fromVpnAddrs` (lighthouse.go:1355-1363) — a "did the host send this about itself" check, not an underlay-address allow-list check. It then unconditionally calls:
```
am.unlockedSetV4(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
am.unlockedSetV6(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
am.unlockedSetRelay(fromVpnAddrs[0], relays)
``` [1](#0-0) 

`unlockedShouldAddV4`/`unlockedShouldAddV6` are the local-address filtering callbacks (governing whether a locally-scoped address should be cached), not the `remote_allow_list` mechanism — the `remote_allow_list` is only consulted via `remoteAllowList.Allow(...)`, which appears exclusively in `handleHostPunchNotification`:
```
remoteAllowList := lhh.lh.GetRemoteAllowList()
...
if remoteAllowList.Allow(detailsVpnAddr, b.Addr()) {
    lhh.lh.punchy.Schedule(b, detailsVpnAddr)
}
``` [2](#0-1) 

Because a peer with a valid handshake (any authenticated but otherwise unprivileged node) can send arbitrary `V4AddrPorts`/`V6AddrPorts` values inside a `HostUpdateNotification`, and the code path that ingests them into the shared `RemoteList` cache never checks `remote_allow_list`, an attacker can insert underlay addresses that the operator explicitly intended to exclude (e.g., private/loopback/metadata-service ranges or specific CIDRs) into the lighthouse's authoritative address cache for that VPN address.

### Impact Explanation
This results in remote-state poisoning of the lighthouse's `RemoteList` cache: other legitimate hosts querying the lighthouse for the attacker's VPN address can be steered toward attacker-favored, disallowed underlay addresses (e.g., ones intended to be filtered for spoofing/SSRF-adjacent or network-segmentation reasons via `lighthouse.remote_allow_list`). This matches the "remote hostmap/state poisoning" bounty impact category, since it undermines the address-filtering invariant that `remote_allow_list` is meant to enforce uniformly across every packet type that updates the RemoteList.

### Likelihood Explanation
Any node with a legitimate handshake to the lighthouse (no CA compromise or privileged access required) can send a crafted `HostUpdateNotification` with attacker-chosen `V4AddrPorts`/`V6AddrPorts` values; the only gate is that `detailsVpnAddr`, if set, must be one of its own `fromVpnAddrs`, which is trivially satisfiable by a normal authenticated peer. This makes the issue readily and repeatably triggerable by any authenticated-but-untrusted node, independent of the lighthouse operator's `remote_allow_list` configuration.

### Recommendation
Apply the same `lhh.lh.GetRemoteAllowList()` / `remoteAllowList.Allow(vpnAddr, addr)` filtering in `handleHostUpdateNotification` before calling `am.unlockedSetV4`/`unlockedSetV6` (and for relay addresses if applicable), mirroring the enforcement already present in `handleHostPunchNotification`, so that every code path that mutates the RemoteList honors `lighthouse.remote_allow_list` consistently.

### Proof of Concept
1. Configure a lighthouse with `lighthouse.remote_allow_list` denying a specific CIDR (e.g., `192.0.2.0/24`).
2. Construct a `NebulaMeta_HostUpdateNotification` from an authenticated peer whose `fromVpnAddrs[0]` is valid, with `n.Details.V4AddrPorts` containing an address inside the denied CIDR.
3. Call `LightHouseHandler.handleHostUpdateNotification(n, fromVpnAddrs, w)` directly (unit test) or drive it through the full receive path.
4. Assert that the lighthouse's `RemoteList` for that VPN address (via `unlockedGetRemoteList`) does NOT contain the denied address — expected failure with current code: the address IS present, confirming the allow-list is bypassed.
5. Compare against an equivalent `handleHostPunchNotification` test with the same denied address, where `remoteAllowList.Allow` correctly prevents scheduling, to demonstrate the asymmetry.

### Citations

**File:** lighthouse.go (L1372-1374)
```go
	am.unlockedSetV4(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
	am.unlockedSetV6(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
	am.unlockedSetRelay(fromVpnAddrs[0], relays)
```

**File:** lighthouse.go (L1427-1435)
```go
	remoteAllowList := lhh.lh.GetRemoteAllowList()
	for _, a := range n.Details.V4AddrPorts {
		if a == nil {
			continue
		}
		b := protoV4AddrPortToNetAddrPort(a)
		if remoteAllowList.Allow(detailsVpnAddr, b.Addr()) {
			lhh.lh.punchy.Schedule(b, detailsVpnAddr)
		}
```
