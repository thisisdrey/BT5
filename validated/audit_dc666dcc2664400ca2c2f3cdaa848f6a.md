### Title
Firewall conntrack revalidation silently skipped after `rulesVersion` wraps around, allowing stale entries to bypass firewall rule changes - ([File: firewall.go])

### Summary
Nebula's connection tracker records which firewall rule-set version allowed a given flow (`conn.rulesVersion`) and compares it against the live firewall's `rulesVersion` on every packet to decide whether the entry must be re-validated against the current rule set [1](#0-0) . This is structurally the same "global counter vs. per-entry counter" pattern described in the external report: a single global tracker (`Firewall.rulesVersion`) is compared against many individual, per-flow trackers (`conn.rulesVersion`), and equality between the two is used to decide whether to trust old state instead of re-checking it.

### Finding Description
`rulesVersion` is only a `uint16` and is incremented by one on every firewall reload that changes the ruleset [2](#0-1) . The only overflow protection implemented is for the exact moment the counter wraps to `0`, in which case the conntrack table is dropped instead of carried over: [3](#0-2) 

For every other wrap value (1, 2, 3, … 65535) the old `Conntrack` table is carried forward unconditionally. Since the counter is modulo 65536, after a full cycle a brand-new firewall's `rulesVersion` will eventually collide with the `rulesVersion` recorded on an old, still-unexpired conntrack entry that was created under a completely different (and possibly much more restrictive) rule set. The revalidation logic in `inConns`/`Firewall.Drop` only re-checks the entry against the current table when the two values differ: [4](#0-3) 

When `c.rulesVersion == f.rulesVersion` purely by modular coincidence (not because the entry was actually created under the current ruleset), the code takes the `else` branch implicitly — it just refreshes the expiry and returns "allowed" without ever calling `table.match(...)` against the live rules. This mirrors the Sherlock bug pattern exactly: the "global" side of the comparison (`Firewall.rulesVersion`) can be reset/cycle back to a value that a stale "individual" tracker (`conn.rulesVersion`) still holds, and the code trusts that coincidental equality instead of re-deriving trust from the current authoritative state.

### Impact Explanation
If exploited/triggered, this allows previously-permitted traffic tuples to keep bypassing the firewall's current rule set indefinitely, even after an administrator has tightened or completely rewritten the rules, because the stale conntrack entry is never re-validated. This is a concrete firewall enforcement bypass: packets that should be dropped under the new ruleset are allowed simply due to a 16-bit counter collision.

### Likelihood Explanation
This requires roughly 65536 rule-changing reloads to accumulate on a single long-lived node before a collision (other than the already-handled exact-zero case) can occur, and a matching conntrack entry with a sufficiently long timeout (`TCPTimeout` can be days) that survives across that many reloads. This makes it a low-likelihood but real latent design gap rather than an immediately attacker-triggerable bug — the guard only covers the `rulesVersion == 0` case and not the general modular-wraparound case.

### Recommendation
Widen `rulesVersion` (e.g., to a 32/64-bit monotonically increasing counter) so a practical wraparound is infeasible, or track a global "epoch" that changes with every reload cycle and is compared for strict monotonic freshness (e.g., always re-validate any counter value from a previous epoch), rather than relying on raw modular equality between the global and per-entry counters.

### Proof of Concept
1. Start a Nebula node with a firewall rule allowing a given flow; let a conntrack entry get created for that flow with `rulesVersion = N`.
2. Trigger 65536 firewall-changing reloads (e.g., toggling a rule) so that `Firewall.rulesVersion` wraps back around to `N` again, while ensuring the original conntrack entry (with a long TCP timeout) has not expired.
3. Change the firewall ruleset so that the previously-allowed flow would now be dropped under the new rules.
4. Send the same flow tuple again — `inConns` sees `c.rulesVersion == f.rulesVersion` (both equal `N` by coincidence) and returns `true` without calling `table.match`, so the packet is incorrectly allowed through the firewall despite the new, stricter rule set: [5](#0-4)

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

**File:** firewall.go (L505-561)
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

**File:** interface.go (L408-424)
```go
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
```
