### Title
Per-routine conntrack cache bypasses firewall rule reload validation - (File: firewall.go, firewall/cache.go)

### Summary
Nebula's stateful firewall re-validates existing conntrack entries against the current rule set whenever `Firewall.rulesVersion` changes, but this re-validation is only performed against the shared, mutex-protected `Conntrack.Conns` map. When Nebula runs with multiple routines, each routine is given a private `firewall.ConntrackCache` (a plain `map[Packet]struct{}` with no `rulesVersion` field) that is checked *before* the shared map and, on a hit, short-circuits the whole `Drop()`/`inConns()` path with `return true`, skipping the rule-version comparison entirely. Because this local cache is only invalidated by a periodic ticker (independent of firewall reload events), previously-permitted traffic tuples continue to be admitted for up to the configured `firewall.conntrack.routine_cache_timeout` interval after an operator tightens the firewall policy, even though the global conntrack table would have correctly dropped them.

### Finding Description
`Firewall.inConns` first checks the caller-supplied `localCache`: [1](#0-0) 

If the tuple is present, it returns `true` immediately without ever touching `conntrack.Conns` or comparing `c.rulesVersion != f.rulesVersion`. Only when there is no local cache hit does the code fall through to the authoritative path, which re-validates stale entries against the current rules on every firewall reload: [2](#0-1) 

After a hit against the authoritative map, the tuple is written back into `localCache`: [3](#0-2) 

`addConn` records the `rulesVersion` at admission time in the shared map only, `conn.rulesVersion`; the per-routine `ConntrackCache` has no such field, so it cannot ever be “stale-checked” — it is either present (=allow) or absent: [4](#0-3) 

The per-routine cache's only invalidation mechanism is a background ticker that wipes the whole map on a fixed interval, unrelated to firewall rule reload: [5](#0-4) 

That interval defaults to `1 * time.Second` whenever more than one routine is configured, and is fully attacker/operator-configurable via `firewall.conntrack.routine_cache_timeout`: [6](#0-5) 

So the sequence enabling bypass is:
1. A remote peer with an existing, permitted flow keeps that flow's `firewall.Packet` tuple cached in the local per-routine `ConntrackCache` of the routine handling its traffic (e.g. by sending periodic keepalive/data packets).
2. The operator tightens the firewall policy (removes/narrows a rule) and reloads config, bumping `Firewall.rulesVersion`.
3. The shared conntrack table would now reject this tuple on next lookup (because `inConns` detects the version mismatch and calls `table.match`), but the routine's local cache still has the stale hit and returns `true` in the branch at firewall.go:507-509, never reaching the version-check logic at all.
4. Traffic that should have been dropped under the new (more restrictive) rule set continues to be forwarded until the next cache tick fires and clears the map.

### Impact Explanation
This is a firewall-enforcement bypass: after a legitimate policy tightening (e.g., revoking access for a group/host/CIDR, blocking a previously allowed port), already-established flows can continue to traverse the tunnel and reach the TUN device for a bounded but attacker-relevant window, undermining the security guarantee that firewall reload takes effect. In multi-routine deployments (which the code explicitly optimizes for and enables by default once `routines > 1`), this window recurs on every packet cadence that keeps hitting the per-routine cache, and is proportional to the configured/default tick interval.

### Likelihood Explanation
The `routine_cache_timeout` optimization is automatically enabled with a non-zero default (1s) as soon as `routines > 1` is configured, which is a documented, supported deployment mode, not a rare corner case. Any remote peer already holding an active connection at the moment of a firewall reload can trigger this without any special privileges — it simply needs to keep sending traffic that matches its previously cached 5-tuple.

### Recommendation
Store the admitting `rulesVersion` alongside each entry in the per-routine `ConntrackCache` (mirroring `conn.rulesVersion` in the shared table), and check it against `f.rulesVersion` before short-circuiting in `inConns`, invalidating/re-validating on mismatch exactly as the shared-map path does. Alternatively, force an immediate full flush of all live `ConntrackCacheTicker` instances whenever `Firewall.rulesVersion` is incremented (config/firewall reload), rather than relying solely on the independent periodic tick.

### Proof of Concept
1. Configure Nebula with `routines: 2` (or more) so `firewall.conntrack.routine_cache_timeout` defaults to `1s` per `main.go:100-104`.
2. Establish a permitted UDP/TCP flow from a peer that matches an existing inbound/outbound rule; keep sending packets on the same 5-tuple so it stays present in that routine's `ConntrackCache`.
3. Reload the Nebula config with a stricter rule set that would now reject this tuple (removing the group/host/cidr that previously allowed it), causing `Firewall.rulesVersion` to increment.
4. Continue sending packets on the same tuple within the cache tick window (up to 1s by default, longer if configured): observe that `Firewall.Drop` returns `nil` (allowed) via the `localCache` hit in `firewall.go:507-509`, even though a lookup against the shared `conntrack.Conns` map would re-validate and reject it per `firewall.go:527-548`.
5. After the next cache tick resets the local map (`firewall/cache.go:52-64`), the same tuple is now correctly evaluated against the shared table and dropped, confirming the earlier window was a transient enforcement bypass.

### Citations

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

**File:** firewall.go (L571-577)
```go
	conntrack.Unlock()

	if localCache != nil {
		localCache[fp] = struct{}{}
	}

	return true
```

**File:** firewall.go (L580-607)
```go
func (f *Firewall) addConn(fp firewall.Packet, incoming bool) {
	var timeout time.Duration
	c := &conn{}

	switch fp.Protocol {
	case firewall.ProtoTCP:
		timeout = f.TCPTimeout
	case firewall.ProtoUDP:
		timeout = f.UDPTimeout
	default:
		timeout = f.DefaultTimeout
	}

	conntrack := f.Conntrack
	conntrack.Lock()
	if _, ok := conntrack.Conns[fp]; !ok {
		conntrack.TimerWheel.Advance(time.Now())
		conntrack.TimerWheel.Add(fp, timeout)
	}

	// Record which rulesVersion allowed this connection, so we can retest after
	// firewall reload
	c.incoming = incoming
	c.rulesVersion = f.rulesVersion
	c.Expires = time.Now().Add(timeout)
	conntrack.Conns[fp] = c
	conntrack.Unlock()
}
```

**File:** firewall/cache.go (L37-67)
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

**File:** main.go (L97-107)
```go
	// EXPERIMENTAL
	// Intentionally not documented yet while we do more testing and determine
	// a good default value.
	conntrackCacheTimeout := c.GetDuration("firewall.conntrack.routine_cache_timeout", 0)
	if routines > 1 && !c.IsSet("firewall.conntrack.routine_cache_timeout") {
		// Use a different default if we are running with multiple routines
		conntrackCacheTimeout = 1 * time.Second
	}
	if conntrackCacheTimeout > 0 {
		l.Info("Using routine-local conntrack cache", "duration", conntrackCacheTimeout)
	}
```
