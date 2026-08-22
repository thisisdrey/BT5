### Title
Local conntrack cache bypasses firewall rule-reload revalidation, allowing traffic that should be dropped after a ruleset change - (File: `firewall.go`, `firewall/cache.go`)

### Summary
`Firewall.inConns()` in `firewall.go` checks a per-goroutine `localCache` before consulting the shared conntrack table's `rulesVersion` revalidation logic. Once a flow tuple is inserted into `localCache`, subsequent packets for that tuple are approved unconditionally until the cache is time-based reset, without ever checking whether the firewall ruleset has since been reloaded.

### Finding Description
`Firewall.Drop()` calls `f.inConns(fp, h, caPool, localCache)` to decide whether a packet belongs to an already-allowed connection [1](#0-0) . Inside `inConns`, the very first check is against the caller-supplied `localCache`; if the tuple is present, the function returns `true` immediately, short-circuiting everything below it: [2](#0-1) .

The logic that is skipped is exactly the mechanism designed to keep conntrack decisions correct across a rule reload: the shared `conntrack.Conns[fp]` entry carries a `rulesVersion`, and if it doesn't match the firewall's current `f.rulesVersion`, the entry is re-validated against the *new* rule table (and dropped/evicted if it no longer matches) [3](#0-2) . This is the same mechanism exercised by `TestFirewall_DropConntrackReload`, which shows that after a reload, previously allowed conntrack-based traffic is supposed to be dropped once the new ruleset no longer permits it [4](#0-3) .

However, `localCache` is only invalidated on a time tick, not on a firewall ruleset change. `ConntrackCacheTicker.Get()` resets the map only when a background ticker (`conntrack_cache_timeout`) has advanced to a new tick value; it has no knowledge of `rulesVersion` at all: [5](#0-4) . The ticker goroutine advances independently of `reloadFirewall`, which builds an entirely new `Firewall` object (with a bumped `rulesVersion`) but never touches the per-reader `ConntrackCacheTicker` instances living in `listenIn`/`listenOut` [6](#0-5) [7](#0-6) .

This is structurally the same bug class as the referenced report: a security-relevant decision (`utilization` / here, "is this packet part of an allowed connection") is computed from a value (`_totalBorrow.elastic` / here, `localCache`) that fails to incorporate a state update that has already happened elsewhere in the same code path (interest accrual / here, `rulesVersion` bump from a firewall reload), producing an under-strict (or here, stale-permissive) result used to gate an important control (`utilization > maximumTargetUtilization` / here, `ErrNoMatchingRule` enforcement).

### Impact Explanation
If an operator tightens or reloads the firewall (removing a previously allowed rule, e.g. to cut off a compromised or newly-restricted peer) while an existing flow's tuple is already resident in a reader goroutine's `localCache`, packets on that flow continue to be forwarded unconditionally for up to `conntrack_cache_timeout` (a configurable, potentially multi-second/longer window) after the reload — the entry is never revalidated against the new rules within that cache's lifetime, because `inConns` never reaches the `rulesVersion` check. This is a firewall-bypass condition reachable purely by an already-connected remote peer (no valid certificate beyond what was already accepted) continuing to send along an existing flow tuple after an admin-initiated tightening of policy — i.e., traffic that the new ruleset is supposed to reject can continue to pass.

### Likelihood Explanation
This triggers deterministically any time `firewall.reload_interval`/config reload changes rules while `conntrack_cache_timeout` is non-zero (the default packet-processing path uses this cache per reader goroutine) and an active flow's tuple is already cached locally; no attacker action beyond maintaining an existing connection is required to benefit from the stale window, making the likelihood of the discrepancy occurring high in any deployment that both uses the local conntrack cache and reloads the firewall.

### Recommendation
Tie the local `ConntrackCache` invalidation to `Firewall.rulesVersion` in addition to the time-based tick — e.g., pass or embed the current `rulesVersion` into `ConntrackCacheTicker.Get()`/`inConns` and reset/bypass the local cache whenever the caller's captured `rulesVersion` differs from `f.rulesVersion`, so a reload immediately forces re-validation through the authoritative conntrack table (mirroring the `c.rulesVersion != f.rulesVersion` check already used for the shared `conntrack.Conns` table) instead of allowing the fast-path `localCache` to shadow that check for the remainder of the cache's tick interval.

### Proof of Concept
1. Configure Nebula with `conntrack_cache_timeout` set to a non-trivial value (e.g. several seconds) so `NewConntrackCacheTicker` is active for the packet-reading routines [8](#0-7) .
2. Establish a permitted UDP/TCP flow between two hosts matching an existing firewall rule; the tuple gets inserted both into the shared conntrack table (`addConn`) and, via `inConns`, into the reader's `localCache` [9](#0-8) .
3. While packets for that flow are still arriving within the current cache tick window, reload the firewall config to remove/tighten the rule that previously matched this flow; `reloadFirewall` swaps in a new `Firewall` with `rulesVersion = oldFw.rulesVersion + 1` [10](#0-9) .
4. Continue sending packets for the same tuple: `Drop()` → `inConns()` finds the tuple still present in `localCache` and returns `true` immediately, bypassing the `rulesVersion` mismatch check that would otherwise have re-tested/dropped the flow against the new rules, as demonstrated by the intended behavior in `TestFirewall_DropConntrackReload` which only exercises the shared-table path (no `localCache`) [11](#0-10) .
5. The flow continues to pass until the local cache's tick boundary is crossed, confirming the bypass window.

### Citations

**File:** firewall.go (L459-462)
```go
	// Check if we spoke to this tuple, if we did then allow this packet
	if f.inConns(fp, h, caPool, localCache) {
		return nil
	}
```

**File:** firewall.go (L505-510)
```go
func (f *Firewall) inConns(fp firewall.Packet, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) bool {
	if localCache != nil {
		if _, ok := localCache[fp]; ok {
			return true
		}
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

**File:** firewall.go (L573-578)
```go
	if localCache != nil {
		localCache[fp] = struct{}{}
	}

	return true
}
```

**File:** firewall_test.go (L700-729)
```go
	fw := NewFirewall(l, time.Second, time.Minute, time.Hour, c.Certificate)
	require.NoError(t, fw.AddRule(true, firewall.ProtoAny, 0, 0, []string{"any"}, "", "", "", "", ""))
	cp := cert.NewCAPool()

	// Drop outbound
	assert.Equal(t, fw.Drop(p, false, &h, cp, nil), ErrNoMatchingRule)
	// Allow inbound
	resetConntrack(fw)
	require.NoError(t, fw.Drop(p, true, &h, cp, nil))
	// Allow outbound because conntrack
	require.NoError(t, fw.Drop(p, false, &h, cp, nil))

	oldFw := fw
	fw = NewFirewall(l, time.Second, time.Minute, time.Hour, c.Certificate)
	require.NoError(t, fw.AddRule(true, firewall.ProtoAny, 10, 10, []string{"any"}, "", "", "", "", ""))
	fw.Conntrack = oldFw.Conntrack
	fw.rulesVersion = oldFw.rulesVersion + 1

	// Allow outbound because conntrack and new rules allow port 10
	require.NoError(t, fw.Drop(p, false, &h, cp, nil))

	oldFw = fw
	fw = NewFirewall(l, time.Second, time.Minute, time.Hour, c.Certificate)
	require.NoError(t, fw.AddRule(true, firewall.ProtoAny, 11, 11, []string{"any"}, "", "", "", "", ""))
	fw.Conntrack = oldFw.Conntrack
	fw.rulesVersion = oldFw.rulesVersion + 1

	// Drop outbound because conntrack doesn't match new ruleset
	assert.Equal(t, fw.Drop(p, false, &h, cp, nil), ErrNoMatchingRule)
}
```

**File:** firewall/cache.go (L50-66)
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
```

**File:** interface.go (L339-359)
```go
func (f *Interface) listenIn(reader io.ReadWriteCloser, i int) {
	packet := make([]byte, mtu)
	out := make([]byte, mtu)
	fwPacket := &firewall.Packet{}
	nb := make([]byte, 12, 12)

	conntrackCache := firewall.NewConntrackCacheTicker(f.ctx, f.l, f.conntrackCacheTimeout)

	for {
		n, err := reader.Read(packet)
		if err != nil {
			// Same shutdown noise handling as listenOut
			if !f.closed.Load() && f.ctx.Err() == nil {
				f.l.Error("Error while reading outbound packet, closing", "error", err, "reader", i)
				f.onFatal(err)
			}
			break
		}

		f.consumeInsidePacket(packet[:n], fwPacket, nb, out, i, conntrackCache.Get())
	}
```

**File:** interface.go (L386-426)
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
```
