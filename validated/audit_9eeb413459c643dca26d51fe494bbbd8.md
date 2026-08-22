### Title
Firewall local conntrack cache trusts stale allow-decisions across rule reload, bypassing re-validation - (File: firewall.go)

### Summary
The Sherlock report describes a class of bug where a security-relevant decision (whether a position is liquidable) is made using stale cached state (`total_debt_shares`) instead of refreshing it first, letting an attacker perform an action (`remove_margin`) that should have been blocked. The reachable analog in this codebase is `Firewall.inConns` / `Firewall.Drop`, where the per-packet-batch `ConntrackCache` (`localCache`) short-circuits the firewall's own stale-state defense (the `rulesVersion` check) that exists specifically to re-validate conntrack entries after a rule reload.

### Finding Description
`Firewall.Drop` calls `f.inConns(fp, h, caPool, localCache)` to decide whether an already-seen flow may continue without a full rule match. [1](#0-0) 

Inside `inConns`, the *local* cache is checked first and, on a hit, returns `true` immediately, before the conntrack-table path that re-checks `c.rulesVersion != f.rulesVersion` and re-runs `table.match(...)` against the current ruleset when the version has changed: [2](#0-1) 

The `rulesVersion` field on each `conn` entry, and the comparison in `inConns`, exist precisely to protect against a stale allow-decision surviving a firewall rule reload — this is the codebase's own mitigation for the same bug class as M-1 (a cached "is this allowed" state that must be revalidated once the underlying policy/state changes): [3](#0-2) [4](#0-3) 

However, `localCache` (`firewall.ConntrackCache`, a `map[Packet]struct{}`) carries no `rulesVersion` field at all, and its `Get()` accessor only resets the map when a background ticker tick has advanced — not when the firewall's `rulesVersion` changes: [5](#0-4) [6](#0-5) 

So once a flow tuple is inserted into `localCache` (which happens whenever the slower conntrack path returns `true`, including right after a rulesVersion re-validation): [7](#0-6) 

...any packet for that same tuple that arrives before the next cache-ticker tick will be allowed purely from `localCache[fp]` with **no** re-check of `rulesVersion` and **no** call to `table.match`, even if a firewall reload happened in between and changed/removed the rule that originally allowed the flow.

### Impact Explanation
This mirrors M-1's root cause exactly: a permission/authorization decision (packet forwarding) is made from a value that is not refreshed against the latest authoritative state (the current rule set / `rulesVersion`) before the decision is used. The practical effect is a firewall-rule-reload bypass window: traffic that should be dropped under the newly loaded rules can still pass for the duration of the local cache's validity, undermining the explicit re-validation mechanism (`rulesVersion`) that the rest of `inConns` was designed to enforce.

### Likelihood Explanation
This is reachable by any peer with an already-established (previously-allowed) flow — no CA-signed certificate beyond the normal peer certificate is required, and no malicious-peer/lighthouse trust is needed. It triggers whenever an operator reloads/tightens firewall rules while existing sessions have in-flight batches sharing a `ConntrackCache`, which is a realistic, non-attacker-controlled but security-relevant operational sequence (config reload racing live traffic).

### Recommendation
Tie `localCache` entries to the firewall's current `rulesVersion` (e.g., store `(fp, rulesVersion)` or invalidate/clear the whole `ConntrackCache` synchronously whenever `Firewall.rulesVersion` is bumped on reload) so a local-cache hit can never skip the same re-validation that conntrack-table hits are subject to.

### Proof of Concept
1. Establish a flow that is allowed by rule R1; `inConns` records it in `conntrack.Conns` with `rulesVersion = v1` and also inserts it into the caller's `localCache`.
2. While the ticker has not yet advanced (i.e., `localCache` not reset), reload the firewall config removing/blocking that flow, bumping `f.rulesVersion` to `v2`.
3. Send another packet for the same tuple within the same `localCache` lifetime: `inConns` returns `true` from `localCache[fp]` immediately, never reaching the `c.rulesVersion != f.rulesVersion` branch, so the packet is forwarded despite the new ruleset disallowing it.

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

**File:** firewall.go (L459-462)
```go
	// Check if we spoke to this tuple, if we did then allow this packet
	if f.inConns(fp, h, caPool, localCache) {
		return nil
	}
```

**File:** firewall.go (L505-560)
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
```

**File:** firewall.go (L570-578)
```go

	conntrack.Unlock()

	if localCache != nil {
		localCache[fp] = struct{}{}
	}

	return true
}
```

**File:** firewall/cache.go (L10-20)
```go
// ConntrackCache is used as a local routine cache to know if a given flow
// has been seen in the conntrack table.
type ConntrackCache map[Packet]struct{}

type ConntrackCacheTicker struct {
	cacheV    uint64
	cacheTick atomic.Uint64

	l     *slog.Logger
	cache ConntrackCache
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
