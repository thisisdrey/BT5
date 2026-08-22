### Title
`NewHostnameResults` DNS resolution failures silently overwrite previously-cached, correct static-host addresses - ([File: remote_list.go])

### Summary
Nebula's `RemoteList` caches the resolved underlay (public) addresses for each configured `static_host_map` hostname entry so the data plane knows where to send handshake/traffic packets. Resolution of hostnames happens in a background goroutine inside `NewHostnameResults`. When a lookup for one of the configured hostnames fails, the code simply `continue`s past that entry and still commits the resulting (now incomplete) address set to the shared cache if it differs from what was previously stored — exactly the pattern flagged in the external report, where a call that returns an *error* result is nonetheless allowed to overwrite a previously-correct cached value instead of being discarded/reverted.

### Finding Description
In `remote_list.go`, `NewHostnameResults` launches a goroutine that periodically re-resolves every configured `hostPort` for a static-host entry: [1](#0-0) 

For each hostname/IP in the list it calls `net.DefaultResolver.LookupNetIP`. If that call returns an error (`err != nil`), the code logs it and `continue`s — it does **not** abort the update or fall back to keeping the previously known-good entries for the whole set: [2](#0-1) 

After the loop, the freshly built (and now potentially incomplete) `netipAddrs` set is compared against the previous `origSet`, and whenever they differ — which they will if any lookup failed while others succeeded, or if all failed leaving an empty set — the new, degraded set unconditionally replaces the old one and callers are notified: [3](#0-2) 

The stored result (`r.ips`) directly feeds `GetAddrs()`, which is consumed by `addStaticRemotes` to populate the `RemoteList` used by the handshake manager and lighthouse code to decide which underlay addresses are trusted for a given VPN peer: [4](#0-3) 

This mirrors the reported bug class precisely: an operation that can legitimately fail for reasons outside the caller's control (an `outOfGas` precompile call in the original report; a DNS resolution failure here) is still allowed to commit its result into a mapping that is relied upon for correctness, overwriting data that was previously valid — rather than reverting/preserving the last-known-good state on failure.

### Impact Explanation
`RemoteList`/`addrMap` entries are the address-trust source that Nebula uses to decide which underlay IP:port a handshake or roam should be sent to for a given static-host peer (e.g., lighthouses configured via hostname in `static_host_map`). An attacker positioned to interfere with DNS resolution for one of several hostnames configured for a host (e.g. a network-path attacker who can drop or poison DNS answers, requiring no valid Nebula certificate) can cause the cached address set for that host to shrink or become stale/empty even though a previously-correct, fuller set had already been cached. Because the update is unconditional whenever the sets differ, a transient or attacker-induced resolution failure poisons the RemoteList that subsequent handshake and roaming logic depends on, which falls under the accepted "remote state poisoning" impact category.

### Likelihood Explanation
No certificate or authenticated peer status is required — the attacker only needs the ability to disrupt or manipulate DNS resolution reachable by the victim node (e.g., on-path interference, DNS server compromise, or forcing timeouts), which is achievable by any network-adjacent attacker. The failure path (`err != nil` → `continue`) is hit on every DNS timeout/NXDOMAIN and is not rate-limited or otherwise gated, so the condition is easy to trigger repeatedly given the periodic ticker in `NewHostnameResults`.

### Recommendation
On a per-hostname DNS lookup failure, `NewHostnameResults`'s refresh goroutine should retain the previously resolved addresses for that specific hostname (or skip publishing an update altogether if any lookup failed) instead of merging only the successfully resolved subset into `netipAddrs` and unconditionally committing it via `r.ips.Store`. Only a fully successful resolution pass — or an explicit, intentional "hostname no longer resolves" signal — should be allowed to shrink or clear cached addresses.

### Proof of Concept
1. Configure `static_host_map` with a peer host having two entries, e.g. `["good-host.example.com:4242", "1.2.3.4:4242"]`, and let the initial resolution succeed for both, populating `r.ips` with both addresses (verified via `RemoteList.CopyAddrs`).
2. As a network-adjacent attacker, block or poison DNS resolution for `good-host.example.com` (e.g., drop UDP/53 responses or return SERVFAIL) while leaving the literal IP entry resolvable.
3. Wait for the next tick in the background goroutine in `NewHostnameResults` (`remote_list.go:114-166`). Observe that `LookupNetIP` for `good-host.example.com` returns an error, the loop `continue`s, and `netipAddrs` now only contains `1.2.3.4:4242`.
4. Because `netipAddrs` differs from `origSet` (missing the good-host address), `r.ips.Store(&netipAddrs)` executes, discarding the previously valid, correct address for `good-host.example.com` from the cache — even though that address was never invalid, only transiently unresolvable by the local resolver.
5. Confirm via `GetAddrs()`/`RemoteList.CopyAddrs` that the previously-known-good address is now gone from the cache used for subsequent handshake/roam address selection.

### Citations

**File:** remote_list.go (L114-133)
```go
		go func() {
			defer ticker.Stop()
			for {
				netipAddrs := map[netip.AddrPort]struct{}{}
				for _, hostPort := range r.hostnames {
					timeoutCtx, timeoutCancel := context.WithTimeout(ctx, r.lookupTimeout)
					addrs, err := net.DefaultResolver.LookupNetIP(timeoutCtx, r.network, hostPort.name)
					timeoutCancel()
					if err != nil {
						l.Error("DNS resolution failed for static_map host",
							"hostname", hostPort.name,
							"network", r.network,
							"error", err,
						)
						continue
					}
					for _, a := range addrs {
						netipAddrs[netip.AddrPortFrom(a.Unmap(), hostPort.port)] = struct{}{}
					}
				}
```

**File:** remote_list.go (L134-157)
```go
				origSet := r.ips.Load()
				different := false
				for a := range *origSet {
					if _, ok := netipAddrs[a]; !ok {
						different = true
						break
					}
				}
				if !different {
					for a := range netipAddrs {
						if _, ok := (*origSet)[a]; !ok {
							different = true
							break
						}
					}
				}
				if different {
					l.Info("DNS results changed for host list",
						"origSet", origSet,
						"newSet", netipAddrs,
					)
					r.ips.Store(&netipAddrs)
					onUpdate()
				}
```

**File:** lighthouse.go (L596-619)
```go

	hr, err := NewHostnameResults(ctx, lh.l, d, network, timeout, toAddrs, func() {
		// This callback runs whenever the DNS hostname resolver finds a different set of addr's
		// in its resolution for hostnames.
		am.Lock()
		defer am.Unlock()
		am.shouldRebuild = true
	})
	if err != nil {
		return util.NewContextualError("Static host address could not be parsed", m{"vpnAddr": vpnAddr, "entry": i + 1}, err)
	}
	am.unlockedSetHostnamesResults(hr)

	for _, addrPort := range hr.GetAddrs() {
		if !lh.shouldAdd([]netip.Addr{vpnAddr}, addrPort.Addr()) {
			continue
		}
		switch {
		case addrPort.Addr().Is4():
			am.unlockedPrependV4(lh.myVpnNetworks[0].Addr(), netAddrToProtoV4AddrPort(addrPort.Addr(), addrPort.Port()))
		case addrPort.Addr().Is6():
			am.unlockedPrependV6(lh.myVpnNetworks[0].Addr(), netAddrToProtoV6AddrPort(addrPort.Addr(), addrPort.Port()))
		}
	}
```
