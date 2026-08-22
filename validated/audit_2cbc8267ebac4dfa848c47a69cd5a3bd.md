### Title
Local Conntrack Cache (`firewall.ConntrackCache`) Grants Residual Permission Without Re-validating Firewall Rules or Certificate State - (File: `firewall.go`, `firewall/cache.go`)

### Summary
Nebula's stateful firewall allows an existing flow to bypass rule evaluation via two layers of state: the shared `conntrack.Conns` table and a per-routine `firewall.ConntrackCache` local cache. The external report's bug class is "residual permission left active longer than it should be, allowing unauthorized use." The Nebula analog is that the local `ConntrackCache` lookup in `inConns` short-circuits and returns "allowed" purely from `localCache[fp]` membership, with no check against the current `rulesVersion` or the peer's certificate/groups, unlike the path that reads from the shared `conntrack.Conns` table.

### Finding Description
`Firewall.inConns` is the function that decides whether a packet's 5-tuple (`firewall.Packet`) is already permitted by an established connection: [1](#0-0) 

When `localCache` (a `firewall.ConntrackCache`, i.e. a plain `map[Packet]struct{}`) already contains the flow's `fp` key, the function returns `true` immediately — before ever taking the `conntrack` lock, before checking `c.rulesVersion != f.rulesVersion`, and without touching the peer certificate (`h.ConnectionState.peerCert`) at all: [2](#0-1) 

Compare this to the path used when the entry is *not* already in `localCache`: the shared conntrack entry is explicitly re-validated against the current rule table (`f.OutRules`/`f.InRules`) and the peer certificate/groups whenever `c.rulesVersion != f.rulesVersion` (i.e., after a `SIGHUP` firewall reload): [3](#0-2) 

The `ConntrackCache` returned by `ConntrackCacheTicker.Get()` is only reset on a timer tick (default cadence set by `conntrack_cache_timeout`), not synchronously with a firewall reload: [4](#0-3) 

This mirrors the audited pattern in the referenced report: an allowance (here, "packet is permitted") is granted broadly up front and left outstanding ("residual") rather than being re-checked/reset at the point where conditions may have changed. In `_approve`/`_mintFCashPosition`, the residual state was an ERC20 allowance that outlived the operation that required it; here, the residual state is a firewall permission cached in a routine-local map that outlives the rule set/certificate context that justified it, for up to one full cache-tick interval.

### Impact Explanation
If an operator tightens firewall rules or a peer's certificate becomes invalid/blocklisted (rulesVersion bump via `reloadFirewall`), any flow whose 5-tuple was already recorded in a given packet-processing goroutine's `localCache` continues to be treated as allowed for the remainder of that cache generation, independent of the new rule set. This is a remote state poisoning / firewall bypass class of issue: packets that should be dropped under the newly-loaded (more restrictive) rules, or from a host whose certificate/group membership no longer matches, can still be forwarded purely because the local per-routine cache says "already seen," until the next `cacheTick` causes `Get()` to reset the map. Because packet processing routines are per-CPU/per-listener, the exposure window is bounded by `conntrack_cache_timeout` but is nonzero and not synchronized with rule reloads.

### Likelihood Explanation
Exploitation requires only an already-established (previously permitted) flow — it does not require possessing a CA-signed certificate beyond what any peer already has to complete a handshake in the first place, and it does not require a malicious peer/lighthouse. Any already-connected remote host benefits automatically from this residual grant window every time the local operator performs a routine firewall tightening (a normal `SIGHUP` reload), without any additional attacker action, making the likelihood of the stale-permission window occurring high in operational use; the security-relevant question is only whether the window is acceptable, which the code's own rulesVersion-revalidation logic in the shared-conntrack path suggests it should not be.

### Recommendation
Make `inConns`'s local-cache fast path consistent with the shared-conntrack path: store the `rulesVersion` (and/or a certificate fingerprint) alongside the `struct{}` value in `ConntrackCache`, and validate it on every hit, or eliminate the standalone early return and always fall through to the shared-conntrack validation logic so a rules change is honored immediately rather than after the next cache tick.

### Proof of Concept
1. Establish a tunnel/flow between two peers so a firewall rule match records `conntrack.Conns[fp]` and the packet-processing goroutine's `localCache[fp] = struct{}{}` (via `inConns`, `firewall.go:573-575`).
2. While that flow is still within the same `ConntrackCacheTicker` generation (before the next tick fires `cacheTick.Add(1)` in `firewall/cache.go:37-47`), an operator issues a `SIGHUP` that tightens `firewall.inbound`/`outbound` rules or removes the peer's group/CA from the allowed set, triggering `Interface.reloadFirewall` and a `rulesVersion` bump (`interface.go:386-434`).
3. A subsequent packet for the same `fp` handled by that same goroutine hits the `localCache[fp]` branch in `inConns` (`firewall.go:505-510`) and is allowed without ever reaching the `rulesVersion`/certificate re-check that the non-cached path performs (`firewall.go:527-548`) — demonstrating the residual-permission bypass of the newly tightened firewall policy for the remainder of the cache generation.

### Citations

**File:** firewall.go (L505-525)
```go
func (f *Firewall) inConns(fp firewall.Packet, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) bool {
	if localCache != nil {
		if _, ok := localCache[fp]; ok {
			return true
		}
	}
	conntrack := f.Conntrack
	conntrack.Lock()

	// Purge every time we test
	ep, has := conntrack.TimerWheel.Purge()
	if has {
		f.evict(ep)
	}

	c, ok := conntrack.Conns[fp]

	if !ok {
		conntrack.Unlock()
		return false
	}
```

**File:** firewall.go (L527-548)
```go
	if c.rulesVersion != f.rulesVersion {
		// This conntrack entry was for an older rule set, validate
		// it still passes with the current rule set
		table := f.OutRules
		if c.incoming {
			table = f.InRules
		}

		// We now know which firewall table to check against
		if !table.match(fp, c.incoming, h.ConnectionState.peerCert, caPool) {
			if f.l.Enabled(context.Background(), slog.LevelDebug) {
				h.logger(f.l).Debug("dropping old conntrack entry, does not match new ruleset",
					"fwPacket", fp,
					"incoming", c.incoming,
					"rulesVersion", f.rulesVersion,
					"oldRulesVersion", c.rulesVersion,
				)
			}
			delete(conntrack.Conns, fp)
			conntrack.Unlock()
			return false
		}
```

**File:** firewall/cache.go (L50-67)
```go
// Get checks if the cache ticker has moved to the next version before returning
// the map. If it has moved, we reset the map.
func (c *ConntrackCacheTicker) Get() ConntrackCache {
	if c == nil {
		return nil
	}
	if tick := c.cacheTick.Load(); tick != c.cacheV {
		c.cacheV = tick
		if ll := len(c.cache); ll > 0 {
			if c.l.Enabled(context.Background(), slog.LevelDebug) {
				c.l.Debug("resetting conntrack cache", "len", ll)
			}
			c.cache = make(ConntrackCache, ll)
		}
	}

	return c.cache
}
```
