### Title
Firewall rule changes are not immediately enforced due to time-based per-routine conntrack caching - (File: `firewall.go`, `firewall/cache.go`)

### Summary
Nebula's firewall evaluates every packet through `Firewall.Drop`, which first calls `f.inConns(fp, h, caPool, localCache)`. That function checks a per-routine `localCache` (a `firewall.ConntrackCache`) before doing anything else, and if the flow's `Packet` tuple is present, the packet is unconditionally allowed with **no** revalidation against the current firewall ruleset. The revalidation-on-ruleset-change logic (`c.rulesVersion != f.rulesVersion` check inside `inConns`) is only reached when the flow is *not* already present in `localCache`. The local cache itself is reset purely on a wall-clock ticker (`ConntrackCacheTicker.tick`, governed by `firewall.conntrack.cache_timeout`), completely decoupled from whether the firewall ruleset was reloaded/tightened. [1](#0-0) [2](#0-1) [3](#0-2) 

### Finding Description
This is the same bug class as the GMX report: a security-relevant state change (disabling/tightening firewall rules via reload) is expected to immediately take effect, but a piece of state (`localCache`) is tracked purely by wall-clock time and does not account for the "pause"/reload event. In the referenced GMX bug, fees kept accruing by elapsed time even though order creation/execution was disabled; here, previously-allowed flows keep being *passed by the firewall* by elapsed time (via the cache-tick window) even though the admin has just reloaded a stricter ruleset that should immediately block them.

Concretely:
- `Firewall.Drop` calls `f.inConns(fp, h, caPool, localCache)` first [1](#0-0) .
- `inConns` short-circuits to `return true` (allow) if the packet tuple is already in `localCache`, *before* ever checking `f.rulesVersion` against the conn's recorded `rulesVersion` [4](#0-3) .
- Only when the tuple is **not** in `localCache` does the code fall through to the conntrack map lookup and the `c.rulesVersion != f.rulesVersion` revalidation against the live table (`table.match(...)`) [5](#0-4) .
- `localCache` is populated by the caller once per routine/packet-batch and is only invalidated by `ConntrackCacheTicker.Get()`, which resets the map purely based on a fixed-duration ticker independent of any firewall reload event [6](#0-5) .

As a result, for any flow already recorded as active in a worker routine's `localCache`, an operator's `SIGHUP` reload that removes/tightens an inbound or outbound rule (i.e., that "disables" the previously-allowed traffic) will not stop that flow's packets from being passed until the cache-tick timer fires (`firewall.conntrack.cache_timeout`), independent of the ruleset change. This is the exact analog of "position fees are still assessed even though the ability to decrease/act is disabled" — the firewall keeps assessing traffic as allowed purely because time hasn't advanced far enough in the local cache, not because the ruleset says so.

### Impact Explanation
An already-established, previously-allowed flow (e.g., one an attacker or misbehaving peer had permission for under the old ruleset) continues to bypass the firewall's rule enforcement for up to the `cache_timeout` duration after an administrator reloads a stricter/removing rule, defeating the intent of an immediate policy change. This is a firewall-enforcement-bypass condition reachable purely by continuing to send matching traffic through an existing conntrack'd flow, without needing a CA-signed certificate beyond what was already used to establish the pre-existing tunnel.

### Likelihood Explanation
This triggers deterministically any time firewall rules are reloaded while active flows exist that happen to be resident in a worker's `localCache` at the moment of reload — a routine event in long-running Nebula deployments that periodically tighten firewall policy. The window size depends entirely on the configured `firewall.conntrack.cache_timeout`, which can be non-trivial.

### Recommendation
Do not let `localCache` bypass the `rulesVersion` revalidation. Either invalidate/reset the local cache immediately when `f.rulesVersion` changes (e.g., pass the current `rulesVersion` into the cache and compare per lookup, or have `NewFirewallFromConfig`/reload bump a shared cache-generation counter that `ConntrackCacheTicker` observes), or always fall through to the `Conns` map + `rulesVersion` check even on a `localCache` hit so that reloaded rules take effect immediately regardless of the cache-tick timing.

### Proof of Concept
1. Configure Nebula with a firewall rule that allows `tcp/80` inbound from group `g1`.
2. Establish a flow matching that rule; the flow gets tracked in `Conntrack.Conns` and, on the packet-processing routine, cached in `localCache` via `inConns` [7](#0-6) .
3. While packets for that flow keep arriving frequently enough to remain in the same `localCache` generation, reload the config to remove the rule allowing `tcp/80` from `g1` (bumping `f.rulesVersion` via `AddFirewallRulesFromConfig`).
4. Continue sending matching packets: because they hit the `localCache` fast-path in `inConns` before the `ConntrackCacheTicker` rotates, they are passed with `return true` and never reach the `c.rulesVersion != f.rulesVersion` check, so they are not dropped despite the rule removal [2](#0-1) .
5. Only after `firewall.conntrack.cache_timeout` elapses and `ConntrackCacheTicker.Get()` resets the map does the flow get re-evaluated and correctly dropped.

### Citations

**File:** firewall.go (L459-462)
```go
	// Check if we spoke to this tuple, if we did then allow this packet
	if f.inConns(fp, h, caPool, localCache) {
		return nil
	}
```

**File:** firewall.go (L505-518)
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
```

**File:** firewall.go (L520-560)
```go
	c, ok := conntrack.Conns[fp]

	if !ok {
		conntrack.Unlock()
		return false
	}

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

		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			h.logger(f.l).Debug("keeping old conntrack entry, does match new ruleset",
				"fwPacket", fp,
				"incoming", c.incoming,
				"rulesVersion", f.rulesVersion,
				"oldRulesVersion", c.rulesVersion,
			)
		}

		c.rulesVersion = f.rulesVersion
	}
```

**File:** firewall.go (L570-577)
```go

	conntrack.Unlock()

	if localCache != nil {
		localCache[fp] = struct{}{}
	}

	return true
```

**File:** firewall/cache.go (L37-48)
```go
func (c *ConntrackCacheTicker) tick(ctx context.Context, d time.Duration) {
	t := time.NewTicker(d)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			c.cacheTick.Add(1)
		}
	}
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
