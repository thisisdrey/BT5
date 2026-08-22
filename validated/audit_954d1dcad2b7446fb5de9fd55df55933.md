### Title
Per-batch conntrack `localCache` short-circuits firewall rule re-validation on ruleset reload - (File: `firewall.go`)

### Summary
`Firewall.inConns` bypasses the conntrack "does this connection still match the current ruleset" check whenever the packet's flow tuple is already present in the caller-supplied `localCache` (a `firewall.ConntrackCache`), returning `true` immediately without ever consulting `f.rulesVersion`. This is structurally the same bug class as the C4 Buyout finding: a decision (allow/deny, analogous to `fractionPrice`) is cached at one point in time and reused later without re-checking it against the current version of the authoritative state (`f.rulesVersion`, analogous to `totalSupply`) that can change in between.

### Finding Description
`Firewall.Drop` is the enforcement entry point and calls `f.inConns(fp, h, caPool, localCache)`: [1](#0-0) 

Inside `inConns`, the very first thing that happens is a lookup in the caller's `localCache`. If the tuple is present, the function returns `true` (allow) immediately — no lock is taken on the real conntrack table and, critically, `f.rulesVersion` is never consulted for this hit: [1](#0-0) 

Only when the tuple is *not* in `localCache` does the code proceed to the real conntrack table, where the intended safety mechanism lives: each entry records the `rulesVersion` that most recently validated it, and on every lookup that recorded version is compared to the live `f.rulesVersion` and re-validated against the current rule table if they differ: [2](#0-1) 

That real conntrack re-validation logic (added specifically to handle rule changes across a reload, per the CHANGELOG entry "we will maintain the table and just validate that an entry still matches with the new rule set") is exactly the mechanism the bug report's `fractionPrice`/`lastTotalSupply` recommendation maps to. But `localCache` — a `map[Packet]struct{}` populated as a side effect of a *previous* successful `inConns` call within the same processing batch — carries no `rulesVersion` tag at all: [3](#0-2) 

and is only cleared on a coarse periodic tick, not on firewall reload: [4](#0-3) 

`f.rulesVersion` is bumped by `reloadFirewall` whenever `firewall` config changes (e.g., via SIGHUP), and the whole point of that mechanism is that in-flight/previously-recorded conntrack permissions must be re-checked against the new rule set: [5](#0-4) 

But `localCache` entries populated before the reload are trusted unconditionally by `inConns` for the remainder of that ticker interval (`conntrackCacheTimeout`), regardless of how many times `f.rulesVersion` changes in between. `Firewall.EmitStats` even treats `f.rulesVersion` as the authoritative signal for "the rules changed", underscoring that `localCache` is not supposed to be the last word on a flow's admissibility: [6](#0-5) 

This is the direct analog of the audited bug: `fractionPrice` (a cached decision derived from `totalSupply` at one instant) was trusted at buy/sell time without checking whether `totalSupply` had since changed. Here, "packet tuple is allowed" (a cached decision derived from `rulesVersion` at one instant) is trusted for the rest of the local-cache window without checking whether `rulesVersion` has since changed.

### Impact Explanation
If an operator narrows or removes a previously-permissive firewall rule via a config reload (SIGHUP) while active traffic on an already-conntracked flow is being processed by a reader routine, that routine's `localCache` will continue to admit packets for that flow tuple until the routine's `ConntrackCacheTicker` next fires — i.e., for up to `conntrackCacheTimeout` — even though the flow no longer matches any rule in the newly loaded, more restrictive ruleset. This is a firewall-enforcement bypass window: packets that should be dropped under the current rules are forwarded instead, because the local per-routine cache never re-derives its decision from `f.rulesVersion`.

### Likelihood Explanation
This requires no CA-signed certificate and no privileged access — it's a property of the firewall's own internal caching versus reload logic that any already-connected remote peer's ongoing traffic can trigger simply by continuing to send packets across a reload boundary. The condition is deterministic: any SIGHUP/config reload that changes `firewall` rules while traffic is flowing on an established flow, combined with the routine-local `ConntrackCacheTicker` not yet having ticked, reproduces it. The severity is bounded by the fact that it requires an admin-initiated rule change and only affects flows that were already permitted under the old rules, similar to how the original finding required a plausible-but-uncommon module composition.

### Recommendation
Tag each `localCache` entry (or the whole cache) with the `rulesVersion` that was active when it was populated, and in `inConns` compare that stored version against the live `f.rulesVersion` before trusting a `localCache` hit — mirroring the re-validation already done for the real conntrack table. Simplest fix: invalidate/reset the local cache whenever `f.rulesVersion` changes (e.g., have the `ConntrackCacheTicker` also observe `rulesVersion` and reset when it changes, not just on its time-based tick), so a rule reload closes the bypass window immediately rather than after up to `conntrackCacheTimeout`.

### Proof of Concept
1. Establish an allowed UDP flow `(A:port1 -> B:port2)` under a permissive `firewall` outbound rule; `inConns` records it in the real conntrack table and, on the next packet in the same batch, in `localCache` as well (`firewall.go:570-575`).
2. While packets for this flow keep arriving in the same reader-routine batch (so `localCache[fp]` stays populated), reload the config with a tightened `firewall` rule set that no longer matches `(A,port1,B,port2)`. `reloadFirewall` bumps `f.rulesVersion` on the shared `Firewall` object (`interface.go:413-426`).
3. Subsequent packets for that flow tuple hit the `localCache` short-circuit at the top of `inConns` (`firewall.go:505-510`) and are admitted with `nil` from `Drop`, bypassing both the real conntrack `rulesVersion` re-check and the `OutRules`/`InRules` table match — until `ConntrackCacheTicker`'s next tick (bounded by `conntrackCacheTimeout`) resets `localCache` (`firewall/cache.go:52-67`).

### Citations

**File:** firewall.go (L495-503)
```go
func (f *Firewall) EmitStats() {
	conntrack := f.Conntrack
	conntrack.Lock()
	conntrackCount := len(conntrack.Conns)
	conntrack.Unlock()
	metrics.GetOrRegisterGauge("firewall.conntrack.count", nil).Update(int64(conntrackCount))
	metrics.GetOrRegisterGauge("firewall.rules.version", nil).Update(int64(f.rulesVersion))
	metrics.GetOrRegisterGauge("firewall.rules.hash", nil).Update(int64(f.GetRuleHashFNV()))
}
```

**File:** firewall.go (L505-511)
```go
func (f *Firewall) inConns(fp firewall.Packet, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) bool {
	if localCache != nil {
		if _, ok := localCache[fp]; ok {
			return true
		}
	}
	conntrack := f.Conntrack
```

**File:** firewall.go (L520-561)
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

**File:** firewall/cache.go (L10-12)
```go
// ConntrackCache is used as a local routine cache to know if a given flow
// has been seen in the conntrack table.
type ConntrackCache map[Packet]struct{}
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

**File:** interface.go (L386-434)
```go
func (f *Interface) reloadFirewall(c *config.C) {
	cs := f.pki.getCertState()
	curCert := cs.getCertificate(cert.Version2)
	if curCert == nil {
		curCert = cs.getCertificate(cert.Version1)
	}

	// The firewall builds its routableNetworks set from the certificate's UnsafeNetworks at construction.
	// Check to see if that set has changed, and if so, rebuild the firewall.
	certUnsafeChanged := curCert != nil && !slices.Equal(curCert.UnsafeNetworks(), f.firewall.unsafeNetworks)

	if !c.HasChanged("firewall") && !certUnsafeChanged {
		f.l.Debug("No firewall config change detected")
		return
	}

	fw, err := NewFirewallFromConfig(f.l, cs, c)
	if err != nil {
		f.l.Error("Error while creating firewall during reload", "error", err)
		return
	}

	oldFw := f.firewall
	conntrack := oldFw.Conntrack
	conntrack.Lock()
	defer conntrack.Unlock()

	fw.rulesVersion = oldFw.rulesVersion + 1
	// If rulesVersion is back to zero, we have wrapped all the way around. Be
	// safe and just reset conntrack in this case.
	if fw.rulesVersion == 0 {
		f.l.Warn("firewall rulesVersion has overflowed, resetting conntrack",
			"firewallHashes", fw.GetRuleHashes(),
			"oldFirewallHashes", oldFw.GetRuleHashes(),
			"rulesVersion", fw.rulesVersion,
		)
	} else {
		fw.Conntrack = conntrack
	}

	f.firewall = fw

	oldFw.Destroy()
	f.l.Info("New firewall has been installed",
		"firewallHashes", fw.GetRuleHashes(),
		"oldFirewallHashes", oldFw.GetRuleHashes(),
		"rulesVersion", fw.rulesVersion,
	)
}
```
