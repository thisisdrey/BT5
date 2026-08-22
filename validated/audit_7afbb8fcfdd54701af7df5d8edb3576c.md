### Title
Relay VPN addresses learned from a lighthouse/peer bypass `remote_allow_list`, unlike v4/v6 addresses - (File: remote_list.go)

### Summary
`RemoteList` caches three kinds of remote-supplied data per owning VPN address: reported IPv4 addresses, reported IPv6 addresses, and relay VPN addresses. The v4/v6 paths are gated by an allow-list predicate before being accepted; the relay path has no such gate.

### Finding Description
`unlockedSetV4` and `unlockedSetV6` each take a `check` callback and only append an entry when `check(vpnIp, v)` returns true: [1](#0-0) [2](#0-1) 

That `check` function is `LightHouse.unlockedShouldAddV4` / `unlockedShouldAddV6`, which enforces `remoteAllowList` before an address is cached: [3](#0-2) 

`unlockedSetRelay`, by contrast, takes no check function and unconditionally appends every relay VPN address supplied by the caller into the cache: [4](#0-3) 

This function is invoked directly with attacker-influenced protobuf data from `handleHostQueryReply` (lighthouse replying to a query) and `handleHostUpdateNotification` (a host pushing an update), both of which pull `relays := n.Details.GetRelays()` straight off the wire with no allow-list filtering, unlike the sibling `V4AddrPorts`/`V6AddrPorts` calls on the same lines which do pass `lh.unlockedShouldAddV4`/`unlockedShouldAddV6`: [5](#0-4) [6](#0-5) 

This is a structural inconsistency of exactly the same class as the referenced report: two parallel code paths perform the same category of operation (accepting a remote-controlled address into a trusted structure), one enforces a whitelist (`remote_allow_list`), the other does not.

### Impact Explanation
`unlockedCollect` copies the unfiltered relay cache into `RemoteList.relays` with no allow-list re-check at collection time either: [7](#0-6) 

Any host that has completed a normal, certificate-authenticated handshake (i.e., an in-network peer without special privilege, or the lighthouse relaying peer-supplied data) can advertise itself, or any arbitrary VPN address, as a relay for a given owner, and that value is accepted into the local `RemoteList` regardless of `lighthouse.remote_allow_list` configuration. Since `remote_allow_list` is documented and used elsewhere specifically to constrain which underlay/relay information a non-lighthouse node will accept from lighthouses (see the v4/v6 gating and `AllowUnknownVpnAddr`/`AllowAll` checks used for handshakes and roaming: [8](#0-7) [9](#0-8) ), the relay path represents a gap in that trust boundary: relay-address poisoning is not subject to the same operator-configured restriction as direct-address poisoning.

### Likelihood Explanation
The relay list is populated from `HostQueryReply` and `HostUpdateNotification` messages that are processed after only handshake/cert authentication, not after any relay-specific authorization check; any node capable of completing a handshake in the mesh (or a compromised/lighthouse-adjacent node) can trigger this path with no additional privilege, making it readily reachable in a normal deployment.

### Recommendation
Reuse the existing allow-list check pattern from `unlockedSetV4`/`unlockedSetV6`: give `unlockedSetRelay` a `check` parameter (e.g., a relay-specific predicate backed by `remoteAllowList`, or reuse `unlockedShouldAddV4`/V6`-style logic against the relay's VPN address) and have `handleHostQueryReply`/`handleHostUpdateNotification` pass it exactly as they do for `V4AddrPorts`/`V6AddrPorts`.

### Proof of Concept
1. Configure a Nebula node with a restrictive `lighthouse.remote_allow_list` such that address ranges belonging to a would-be malicious relay VPN address are disallowed.
2. As any peer that can complete a normal certificate-authenticated handshake with the target's lighthouse, send a `HostUpdateNotification` (or reply to a `HostQuery`) for some victim VPN address, setting `Details.Relays` to include a VPN address that would be rejected by `remote_allow_list` if it had been supplied as a v4/v6 endpoint.
3. Observe that `lhh.lh.unlockedGetRemoteList(...).unlockedSetRelay(...)` is called with no allow-list check (`remote_list.go:471-480`), and the value is unconditionally stored, then copied into `RemoteList.relays` on the next `unlockedCollect()` (`remote_list.go:620-625`), even though the equivalent v4/v6 address would have been dropped by `unlockedShouldAddV4`/`unlockedShouldAddV6` (`lighthouse.go:712-733`).

### Citations

**File:** remote_list.go (L454-469)
```go
// unlockedSetV4 assumes you have the write lock and resets the reported list of ips for this owner to the list provided
// and marks the deduplicated address list as dirty
func (r *RemoteList) unlockedSetV4(ownerVpnIp, vpnIp netip.Addr, to []*V4AddrPort, check checkFuncV4) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeV4(ownerVpnIp)

	// Reset the slice
	c.reported = c.reported[:0]

	// We can't take their array but we can take their pointers
	for _, v := range to[:minInt(len(to), MaxRemotes)] {
		if check(vpnIp, v) {
			c.reported = append(c.reported, v)
		}
	}
}
```

**File:** remote_list.go (L471-480)
```go
func (r *RemoteList) unlockedSetRelay(ownerVpnIp netip.Addr, to []netip.Addr) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeRelay(ownerVpnIp)

	// Reset the slice
	c.relay = c.relay[:0]

	// We can't take their array but we can take their pointers
	c.relay = append(c.relay, to[:minInt(len(to), MaxRemotes)]...)
}
```

**File:** remote_list.go (L502-517)
```go
// unlockedSetV6 assumes you have the write lock and resets the reported list of ips for this owner to the list provided
// and marks the deduplicated address list as dirty
func (r *RemoteList) unlockedSetV6(ownerVpnIp, vpnIp netip.Addr, to []*V6AddrPort, check checkFuncV6) {
	r.shouldRebuild = true
	c := r.unlockedGetOrMakeV6(ownerVpnIp)

	// Reset the slice
	c.reported = c.reported[:0]

	// We can't take their array but we can take their pointers
	for _, v := range to[:minInt(len(to), MaxRemotes)] {
		if check(vpnIp, v) {
			c.reported = append(c.reported, v)
		}
	}
}
```

**File:** remote_list.go (L620-625)
```go
		if c.relay != nil {
			for _, v := range c.relay.relay {
				relays = append(relays, v)
			}
		}
	}
```

**File:** lighthouse.go (L712-733)
```go
// unlockedShouldAddV4 checks if to is allowed by our allow list
func (lh *LightHouse) unlockedShouldAddV4(vpnAddr netip.Addr, to *V4AddrPort) bool {
	udpAddr := protoV4AddrPortToNetAddrPort(to)
	allow := lh.GetRemoteAllowList().Allow(vpnAddr, udpAddr.Addr())
	if lh.l.Enabled(context.Background(), logging.LevelTrace) {
		lh.l.Log(context.Background(), logging.LevelTrace, "remoteAllowList.Allow",
			"vpnAddr", vpnAddr,
			"udpAddr", udpAddr,
			"allow", allow,
		)
	}

	if !allow {
		return false
	}

	if lh.myVpnNetworksTable.Contains(udpAddr.Addr()) {
		return false
	}

	return true
}
```

**File:** lighthouse.go (L1311-1321)
```go
	relays := n.Details.GetRelays()

	lhh.lh.Lock()
	am := lhh.lh.unlockedGetRemoteList([]netip.Addr{certVpnAddr})
	am.Lock()
	lhh.lh.Unlock()

	am.unlockedSetV4(fromVpnAddrs[0], certVpnAddr, n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
	am.unlockedSetV6(fromVpnAddrs[0], certVpnAddr, n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
	am.unlockedSetRelay(fromVpnAddrs[0], relays)
	am.Unlock()
```

**File:** lighthouse.go (L1365-1375)
```go
	relays := n.Details.GetRelays()

	lhh.lh.Lock()
	am := lhh.lh.unlockedGetRemoteList(fromVpnAddrs)
	am.Lock()
	lhh.lh.Unlock()

	am.unlockedSetV4(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
	am.unlockedSetV6(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
	am.unlockedSetRelay(fromVpnAddrs[0], relays)
	am.Unlock()
```

**File:** handshake_manager.go (L1030-1036)
```go
	if !via.IsRelayed {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(vpnAddrs, via.UdpAddr.Addr()) {
			f.l.Debug("lighthouse.remote_allow_list denied incoming handshake",
				"vpnAddrs", vpnAddrs, "from", via)
			return nil, false, false
		}
	}
```

**File:** outside.go (L264-272)
```go
func (f *Interface) handleHostRoaming(hostinfo *HostInfo, via ViaSender) {
	curRemote := hostinfo.GetRemote()
	if !via.IsRelayed && curRemote != via.UdpAddr {
		if !f.lightHouse.GetRemoteAllowList().AllowAll(hostinfo.vpnAddrs, via.UdpAddr.Addr()) {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				hostinfo.logger(f.l).Debug("lighthouse.remote_allow_list denied roaming", "newAddr", via.UdpAddr)
			}
			return
		}
```
