### Title
Split-brain `RemoteList` state via early return in `unlockedGetRemoteList` for multi-address hosts - (File: lighthouse.go)

### Summary
`LightHouse.unlockedGetRemoteList` looks up an existing `*RemoteList` for a set of VPN addresses (e.g. a dual-stack peer's IPv4+IPv6 addresses), but returns as soon as it finds a match for *any one* address instead of continuing to check/register the remaining addresses. This mirrors the M-01 bug class: a per-key "already initialized" check causes the loop to return instead of continuing on to add missing state for the other members of the set.

### Finding Description
`unlockedGetRemoteList` iterates over `allAddrs` and, on finding the first address already present in `lh.addrMap`, immediately returns that `*RemoteList` (only patching `addrMap[allAddrs[0]]` if the match wasn't at index 0): [1](#0-0) 

If a peer's address set grows over time (e.g. a peer that was previously only IPv4 later re-handshakes with a V2 cert that also advertises an IPv6 address, or vice versa), and one of the addresses in the new `allAddrs` set (say `allAddrs[1]`) already has an entry in `addrMap` from an earlier, narrower registration, the function returns that `RemoteList` early — but never adds `allAddrs[0]`, `allAddrs[2]`, etc. to `addrMap` unless the match happened to be at index 0. This is directly analogous to the `ensureClosure()` bug: the "already registered" check is keyed too coarsely (per one member of the set) and short-circuits registration of the other members that still need it.

Callers such as `handleHostQueryReply` and `handleHostUpdateNotification` rely on `addrMap` being consistent across all of a host's addresses: [2](#0-1) [3](#0-2) 

When `addrMap` ends up split (some addresses pointing at one `*RemoteList` object, others still unmapped or later pointing at a second, independently-created `*RemoteList`), future lookups keyed by the un-registered address will call `NewRemoteList` again and create a second `RemoteList` for the same physical peer: [4](#0-3) 

### Impact Explanation
This causes remote-address/relay learning state (`unlockedSetV4`, `unlockedSetV6`, `unlockedSetRelay`, `LearnRemote`) to be poisoned/inconsistent for one of a multi-homed host's addresses: updates delivered by the lighthouse for one address (e.g. IPv6) can be written into a `RemoteList` that is never consulted when the peer is queried by its other address (e.g. IPv4), or an attacker-influenced update reported for one address could get attributed to the wrong/duplicate `RemoteList` object. This is a remote state-poisoning/inconsistency condition reachable purely through normal lighthouse query/update traffic, without requiring a signed certificate from the attacker beyond being a peer that can trigger a `HostQueryReply`/`HostUpdateNotification` for a multi-address vpn identity — i.e., it's within the "remote state poisoning via lighthouse address trust" category called out as in-scope.

### Likelihood Explanation
Likelihood is **Low-Moderate**: it requires a host with more than one registered vpn address (dual-stack V2 certs) whose `addrMap` entries were populated non-atomically/partially (e.g. address added at a different time than the rest, such as through `unlockedGetRemoteList` being called first with a subset of addresses, as happens in `handleHostQueryReply` which passes only `certVpnAddr` as a single-element slice) before being called again later with the full multi-address set. This is a normal operational sequence for lighthouse-mediated discovery of multi-address peers, not a contrived attacker-only path.

### Recommendation
In `unlockedGetRemoteList`, do not return on the first match. Instead, scan all of `allAddrs` for an existing `*RemoteList`; if found, ensure every address in `allAddrs` is mapped to that same `*RemoteList` in `addrMap` (filling in any missing entries) before returning it, analogous to using `continue` instead of `return` in the `ensureClosure` fix recommended in the referenced report.

### Proof of Concept
Conceptual sequence (Go, using `LightHouse.addrMap`/`unlockedGetRemoteList`):
1. Peer `P` first contacts as IPv4-only; `handleHostQueryReply` calls `unlockedGetRemoteList([]netip.Addr{v4})`, creating `RemoteList_1` and setting `addrMap[v4] = RemoteList_1`.
2. `P` later re-handshakes with a V2 cert exposing both `v4` and `v6`; some code path calls `unlockedGetRemoteList([]netip.Addr{v6, v4})` (or `[]netip.Addr{v4, v6}` where `v4` is discovered at index != 0 with `v6` absent). Because the loop returns on the first found match without registering the other address in that call context, `addrMap[v6]` is left unset in scenarios where the discovered index isn't 0 order relative to which entries pre-exist, and any independent call keyed solely by `v6` before that point (e.g., an earlier `unlockedGetRemoteList([]netip.Addr{v6})`) would already have created `RemoteList_2` for `v6`.
3. Subsequent lighthouse updates for `v6` write into `RemoteList_2` while lookups for the peer by `v4` continue to use `RemoteList_1`, producing divergent, stale, or attacker-influenced remote/relay state depending on which address is queried.

Note: I was unable to fully trace every call site that constructs `allAddrs` (in particular whether current call sites always pass full multi-address sets in a way that fully avoids the split, e.g. `unlockedGetRemoteList(fromVpnAddrs)` in `handleHostUpdateNotification` vs. `unlockedGetRemoteList([]netip.Addr{certVpnAddr})` in `handleHostQueryReply`), so the exact triggering sequence's reachability under all cert/version configurations should be verified with a live/e2e test before treating this as a confirmed exploitable path rather than a latent correctness bug in the address-set registration invariant.

### Citations

**File:** lighthouse.go (L672-690)
```go
// unlockedGetRemoteList assumes you have the lh lock
func (lh *LightHouse) unlockedGetRemoteList(allAddrs []netip.Addr) *RemoteList {
	// before we go and make a new remotelist, we need to make sure we don't have one for any of this set of vpnaddrs yet
	for i, addr := range allAddrs {
		am, ok := lh.addrMap[addr]
		if ok {
			if i != 0 {
				lh.addrMap[allAddrs[0]] = am
			}
			return am
		}
	}

	am := NewRemoteList(allAddrs, lh.shouldAdd)
	for _, addr := range allAddrs {
		lh.addrMap[addr] = am
	}
	return am
}
```

**File:** lighthouse.go (L1313-1321)
```go
	lhh.lh.Lock()
	am := lhh.lh.unlockedGetRemoteList([]netip.Addr{certVpnAddr})
	am.Lock()
	lhh.lh.Unlock()

	am.unlockedSetV4(fromVpnAddrs[0], certVpnAddr, n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
	am.unlockedSetV6(fromVpnAddrs[0], certVpnAddr, n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
	am.unlockedSetRelay(fromVpnAddrs[0], relays)
	am.Unlock()
```

**File:** lighthouse.go (L1367-1375)
```go
	lhh.lh.Lock()
	am := lhh.lh.unlockedGetRemoteList(fromVpnAddrs)
	am.Lock()
	lhh.lh.Unlock()

	am.unlockedSetV4(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V4AddrPorts, lhh.lh.unlockedShouldAddV4)
	am.unlockedSetV6(fromVpnAddrs[0], fromVpnAddrs[0], n.Details.V6AddrPorts, lhh.lh.unlockedShouldAddV6)
	am.unlockedSetRelay(fromVpnAddrs[0], relays)
	am.Unlock()
```
