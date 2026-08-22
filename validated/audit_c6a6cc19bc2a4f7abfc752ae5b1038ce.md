### Title
Per-goroutine conntrack cache (`ConntrackCacheTicker`) bypasses firewall ruleset re-validation after a config reload - ([File: firewall.go], [File: firewall/cache.go])

### Summary
This is the same bug class as the referenced `NestedFactory` finding: an operation that changes an authorization state (there: `addOperator`/`removeOperator`; here: a firewall config reload that bumps `Firewall.rulesVersion`) is not atomically reflected in a dependent cache (`operatorCache` there, the per-reader `ConntrackCache` here), so previously-authorized flows keep being allowed even after the authoritative rule set has changed.

### Finding Description
Nebula's `Firewall` already contains an explicit mechanism to prevent conntrack entries from surviving a firewall reload with stale authorization: every `conn` entry stores the `rulesVersion` that was active when it was created, and `inConns()` re-validates the entry against the current rule table whenever `c.rulesVersion != f.rulesVersion`: [1](#0-0) [2](#0-1) 

However, `Drop()` and `inConns()` also accept an optional per-goroutine `localCache firewall.ConntrackCache`, and this cache is checked *before* the conntrack table (and therefore before the `rulesVersion` re-validation) is consulted at all: [3](#0-2) 

Entries are added to this `localCache` only after the `rulesVersion`-aware check succeeds in `inConns()`: [4](#0-3) 

The `localCache` is produced by `ConntrackCacheTicker.Get()`, which only resets the map when a background ticker (`conntrackCacheTimeout`, from `firewall.conntrack_cache_timeout` config) fires — it has no knowledge of, and is not tied to, `Firewall.rulesVersion` or firewall reloads: [5](#0-4) 

This cache is created once per reader routine and lives for the life of the reader loop, being passed into every packet processed on that goroutine: [6](#0-5) 

Meanwhile, `reloadFirewall()` swaps in an entirely new `*Firewall` object (with `rulesVersion` incremented) whenever `firewall.*` config or cert `UnsafeNetworks` changes, specifically to force conntrack entries to be re-validated against the new rules: [7](#0-6) 

Because the `localCache` lookup in `inConns()` short-circuits before the `rulesVersion` comparison is ever performed, a packet 5-tuple that was cached locally under the *old* rule set will continue to be permitted by that reader routine for up to `conntrackCacheTimeout` after a firewall reload — even if the new rule set would deny it (e.g., a rule was removed, groups changed, or CA/host trust was tightened). This mirrors exactly the C4 `NestedFactory` root cause: a state-changing action (`removeOperator`/reload) is not propagated into a cache that a hot-path function (`create()`/`Drop()`) relies on, so the "is this authorized?" check silently uses stale data.

### Impact Explanation
An already-established flow that should be dropped under a newly-reloaded, more restrictive firewall ruleset can continue to pass traffic on a given reader goroutine until its local cache is reset by the ticker. This is a firewall-enforcement bypass: traffic that the administrator just explicitly blocked (by editing `firewall.inbound`/`firewall.outbound` and reloading, e.g. via SIGHUP) can still flow for a bounded but attacker-exploitable window, undermining the atomicity guarantee that `rulesVersion`/`reloadFirewall()` was specifically designed to provide (see `TestFirewall_DropConntrackReload`, which validates the intended re-check behavior at the conntrack-table level but does not exercise the `localCache` short-circuit path). No CA-signed certificate is required beyond what is already needed to establish the original (now-to-be-blocked) tunnel/flow; the exposure is in the firewall-enforcement re-validation path itself.

### Likelihood Explanation
The conntrack `localCache` is used in the default packet-processing hot path (`listenIn`/`listenOut` via `conntrackCache.Get()`), so any environment with `firewall.conntrack_cache_timeout` enabled (non-zero) and any firewall rule change/reload is affected. The race window is bounded by the configured cache timeout but is deterministic and reproducible — no timing luck or privileged position is needed to reproduce; it only requires an existing flow plus a firewall reload that would otherwise deny it.

### Recommendation
Tie the local `ConntrackCacheTicker` invalidation to `Firewall.rulesVersion` rather than (or in addition to) a wall-clock ticker — e.g., pass the current `rulesVersion` into `Get()` and clear the cache whenever it changes, mirroring the invalidation already done for the shared conntrack table. Alternatively, have `inConns()` perform the `rulesVersion` comparison before consulting `localCache`, or invalidate/reset all per-goroutine caches synchronously as part of `reloadFirewall()`'s firewall swap.

### Proof of Concept
1. Start Nebula with `firewall.conntrack_cache_timeout` set to a non-trivial value (e.g. a few seconds) and a permissive rule allowing a given flow.
2. Establish a UDP/TCP flow that matches the rule; the reader goroutine's `localCache` records the flow's `firewall.Packet` key via `inConns()`/`addConn()` after normal rule matching, per `firewall.go` lines 571-578.
3. Continue sending packets belonging to that same flow tuple frequently enough to keep hitting the `localCache` fast path in `inConns()` (`firewall.go` lines 505-510).
4. While the flow is active, edit the config to remove/deny the rule that previously allowed it and trigger a reload (SIGHUP or SSH `reload`), which calls `reloadFirewall()` and installs a new `*Firewall` with an incremented `rulesVersion` (`interface.go` lines 386-434).
5. Because `localCache` still contains the flow's key from before the reload and is only refreshed by the independent ticker (`firewall/cache.go` lines 52-66), packets on that flow continue to be accepted (`inConns()` returns `true` at line 507-508) without ever reaching the `rulesVersion` mismatch check, until the ticker interval elapses and the cache is cleared. [3](#0-2) [8](#0-7)

### Citations

**File:** firewall.go (L30-38)
```go
type conn struct {
	Expires time.Time // Time when this conntrack entry will expire

	// record why the original connection passed the firewall, so we can re-validate
	// after ruleset changes. Note, rulesVersion is a uint16 so that these two
	// fields pack for free after the uint32 above
	incoming     bool
	rulesVersion uint16
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

**File:** firewall.go (L527-560)
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

**File:** firewall.go (L571-578)
```go
	conntrack.Unlock()

	if localCache != nil {
		localCache[fp] = struct{}{}
	}

	return true
}
```

**File:** firewall/cache.go (L22-67)
```go
func NewConntrackCacheTicker(ctx context.Context, l *slog.Logger, d time.Duration) *ConntrackCacheTicker {
	if d == 0 {
		return nil
	}

	c := &ConntrackCacheTicker{
		l:     l,
		cache: ConntrackCache{},
	}

	go c.tick(ctx, d)

	return c
}

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

**File:** interface.go (L339-358)
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
