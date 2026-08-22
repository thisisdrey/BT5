### Title
Stale per-routine conntrack cache bypasses firewall re-validation after a stricter ruleset reload - (File: firewall.go, firewall/cache.go)

### Summary
This mirrors the RewardDistributor bug class: a value (total supply / here, "this 5-tuple is currently permitted") is cached at one point in time, the underlying authoritative state changes (VELO totalSupply grows / here, the firewall ruleset is reloaded to something stricter), but the stale cached value continues to be trusted, producing an incorrect security decision (excess reward / here, packets bypassing the new firewall rules).

### Finding Description
`Firewall.inConns` is the function that decides whether an already-seen flow may bypass full rule matching, based on the global `conntrack.Conns` table and a per-goroutine `localCache firewall.ConntrackCache`: [1](#0-0) 

Critically, the local cache check happens **before** the `rulesVersion` re-validation that the global-conntrack path performs:
```go
func (f *Firewall) inConns(fp firewall.Packet, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) bool {
	if localCache != nil {
		if _, ok := localCache[fp]; ok {
			return true
		}
	}
	...
	if c.rulesVersion != f.rulesVersion {
		// re-validate against current ruleset
	}
``` [2](#0-1) 

The global path does correctly re-validate a stale conntrack entry against the *current* `f.rulesVersion` when rules change, and even deletes the entry if it no longer matches: [3](#0-2) 

However, once a flow has been accepted once, it is stamped into the caller's `localCache` map: [4](#0-3) 

That `localCache` is not owned by the `Firewall` object — it is a long-lived, per-reader-goroutine `ConntrackCacheTicker` created once at startup in `main.go` and handed to `listenIn`/the outside-packet reader for the lifetime of the interface: [5](#0-4) [6](#0-5) 

`ConntrackCacheTicker.Get()` only clears its map when a background ticker (period `d`, default `1s` when `routines > 1`) advances the tick counter: [7](#0-6) 

This cache lifetime is completely independent of `reloadFirewall`, which builds an entirely new `*Firewall` object with an incremented `rulesVersion` on every config/cert change and installs it on the `Interface`: [8](#0-7) 

Because the per-goroutine `localCache` survives across firewall reloads (it belongs to the reader goroutine, not to the `Firewall`), any 5-tuple that was validated and cached under the *old*, more permissive ruleset will continue to short-circuit `inConns` and be treated as allowed — **without ever calling `table.match()` against the new, stricter ruleset** — until the next local-cache tick fires (up to the configured `firewall.conntrack.routine_cache_timeout`, defaulting to 1s when multiple routines are configured).

### Impact Explanation
This is a direct firewall-enforcement bypass: an operator can tighten (or remove) an inbound/outbound firewall rule expecting it to take effect immediately (as documented/intended by `reloadFirewall`'s rule reconciliation logic and the conntrack re-validation added specifically to "validate that an entry still matches with the new rule set" per the CHANGELOG), but for up to the cache-timeout window, already-established connections that were permitted under the old ruleset keep flowing on any goroutine that has already cached that flow, regardless of the new ruleset. This directly undermines the security goal of a live firewall-rule reload — traffic that should now be dropped is still passed through, i.e., a remote peer already communicating over an allowed flow can keep sending/receiving traffic that a freshly-tightened rule was meant to block.

### Likelihood Explanation
The feature (`firewall.conntrack.routine_cache_timeout`) is explicitly enabled by default whenever `routines > 1` is configured (a common performance setting for multi-queue setups), so this is not a corner-case opt-in. Any environment reloading firewall rules to restrict a previously allowed flow while using multi-routine mode is exposed for the cache duration. The bug requires no malicious certificate or lighthouse trust abuse — only a legitimate remote peer that already has an established, cache-hit-eligible flow at the moment rules are tightened, which is a normal operational scenario (e.g., emergency ACL tightening in response to an incident).

### Recommendation
Do not let the per-routine local cache short-circuit rule re-validation across a `rulesVersion` change. Either:
1. Include the current `Firewall.rulesVersion` as part of the local cache key (or store it alongside the cached flow) and invalidate/skip the fast path when it no longer matches the live `Firewall.rulesVersion`, or
2. Reset/version-stamp the per-goroutine `ConntrackCacheTicker` cache whenever `reloadFirewall` swaps in a new `Firewall`, so the stale fast path can never outlive the ruleset it was built against.

### Proof of Concept
Not independently reproduced in a live environment (no runtime/terminal access), but the code path is deterministic from source:
1. Start nebula with `routines > 1` (multi-queue), which defaults `firewall.conntrack.routine_cache_timeout` to `1s`.
2. Establish a UDP/TCP flow that is permitted by the current firewall ruleset; the goroutine handling it calls `fw.Drop(...)` which stores the flow in both the global `conntrack.Conns` and the goroutine-local `ConntrackCache` (`firewall.go:573-577`).
3. Trigger a config/cert reload that tightens the firewall ruleset (`reloadFirewall`, `interface.go:386-434`), producing a new `*Firewall` with `rulesVersion = oldFw.rulesVersion + 1`, whose rules would now reject the flow from step 2.
4. Continue sending packets matching the same 5-tuple on the same reader goroutine within the next `routine_cache_timeout` window (default 1s). `Firewall.Drop` → `inConns` hits the goroutine-local cache first (`firewall.go:506-510`) and returns `true` immediately, bypassing `table.match()` entirely — the packet is passed even though the new ruleset would reject it.
5. After the local ticker advances (>1s), `ConntrackCacheTicker.Get()` resets the map (`firewall/cache.go:56-63`), and subsequent packets fall through to the global conntrack path, which correctly re-validates against `f.rulesVersion` and now drops the flow.

### Citations

**File:** firewall.go (L505-578)
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

	switch fp.Protocol {
	case firewall.ProtoTCP:
		c.Expires = time.Now().Add(f.TCPTimeout)
	case firewall.ProtoUDP:
		c.Expires = time.Now().Add(f.UDPTimeout)
	default:
		c.Expires = time.Now().Add(f.DefaultTimeout)
	}

	conntrack.Unlock()

	if localCache != nil {
		localCache[fp] = struct{}{}
	}

	return true
}
```

**File:** main.go (L100-107)
```go
	conntrackCacheTimeout := c.GetDuration("firewall.conntrack.routine_cache_timeout", 0)
	if routines > 1 && !c.IsSet("firewall.conntrack.routine_cache_timeout") {
		// Use a different default if we are running with multiple routines
		conntrackCacheTimeout = 1 * time.Second
	}
	if conntrackCacheTimeout > 0 {
		l.Info("Using routine-local conntrack cache", "duration", conntrackCacheTimeout)
	}
```

**File:** firewall/cache.go (L14-35)
```go
type ConntrackCacheTicker struct {
	cacheV    uint64
	cacheTick atomic.Uint64

	l     *slog.Logger
	cache ConntrackCache
}

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
