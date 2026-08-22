### Title
Local per-thread conntrack cache allows outbound/inbound flows to bypass firewall rule-reload revalidation for up to a full cache tick - (File: firewall.go, firewall/cache.go)

### Summary
The Sherlock report describes a "close before lazy materialization" bug class: a decision gate (`num_obligation_reward_managers == 0`) is used as a proxy for "no economically-owed state exists," but it is never refreshed against the authoritative accrual before an irreversible action (refund) is taken, so real state is silently discarded/misdirected. Nebula's firewall has a structurally identical pattern: `Firewall.inConns` (`firewall.go:505-578`) is supposed to re-validate every conntrack hit against the current `f.rulesVersion` before allowing a packet, but a caller-supplied `localCache` (`firewall.ConntrackCache`, `firewall/cache.go:1-67`) is checked first and, if the flow's `firewall.Packet` tuple is already present in that local cache, the packet is waved through with **no rulesVersion check at all** (`firewall.go:505-510`). This is the same "stale/lazy proxy used instead of refreshed authoritative state" defect, applied to the firewall enforcement path instead of a reward pool.

### Finding Description
`Firewall.inConns` is the function that decides whether an already-conntracked flow may continue to pass, and it is the mechanism specifically designed to make a previously-allowed flow re-validate against the firewall's rules after a config reload: [1](#0-0) 

Note the structure:
1. If `localCache != nil` and the flow tuple is already a key in `localCache`, `inConns` returns `true` immediately — no lock is taken on `Conntrack`, no comparison of `c.rulesVersion != f.rulesVersion` is performed, and no re-match against `f.InRules`/`f.OutRules` happens.
2. Only when the tuple is *not* in `localCache` does the function proceed to the authoritative path: lock `Conntrack`, look up `conntrack.Conns[fp]`, and — critically — compare `c.rulesVersion != f.rulesVersion` to decide whether the entry must be re-validated against the current rule table (`firewall.go:527-560`).

This authoritative re-validation is exactly the mechanism the changelog documents as intentional and security-relevant ("Reload the firewall when the unsafe networks in the certificate change" / `TestFirewall_DropConntrackReload`), proving the protocol's own design treats `rulesVersion` mismatch as the correct signal that a flow's permission must be re-derived, not assumed: [2](#0-1) 

However, `localCache` — populated only from a **successful pass through the authoritative path** (`firewall.go:573-576`) — is a per-goroutine (`listenIn`/`listenOut` reader) map that is *not* keyed to `rulesVersion` at all. It is only invalidated wholesale on a periodic ticker (`ConntrackCacheTicker`, default cadence set by `firewall.conntrack_cache_timeout`), independent of when a firewall reload actually happens: [3](#0-2) 

`NewFirewall`/reload wiring shows `f.rulesVersion` is bumped and a brand-new `Firewall` object is installed the moment the config or the certificate's unsafe networks change: [4](#0-3) 

But `f.conntrackCacheTimeout`-driven `ConntrackCacheTicker` used in the packet-reading hot loop is a *completely independent, unsynchronized clock*: [5](#0-4) 

So the reload path (`reloadFirewall`) treats "install a new, stricter rule set" as sufficient to enforce the new policy on the next packet for every flow (via the `rulesVersion` bump feeding `inConns`'s authoritative branch), exactly mirroring how the Move report's `close_pool_reward` treats "materialized-tracker count is zero" as sufficient to refund. In both cases the gating check is a *cheap, lazily-updated proxy* (`num_obligation_reward_managers` / `localCache` membership) instead of the *authoritative, freshly-derived value* (`update_pool_reward` accrual / `rulesVersion` comparison), and the proxy can be stale relative to a state transition (borrower already earned reward / operator already reloaded firewall) that already happened but hasn't propagated into the proxy yet.

### Impact Explanation
Because `localCache` bypasses the `rulesVersion` re-check entirely, any flow whose packets keep landing on the same reader goroutine within the same conntrack-cache tick window continues to be treated as allowed under the *old* rule set even after an operator reloads the firewall specifically to revoke that permission (e.g. narrowing `firewall.inbound_action`/`outbound_action`, removing a group/CIDR rule, or the certificate's `unsafe_networks` changing, which the changelog explicitly calls out as reload-triggering: "Reload the firewall when the unsafe networks in the certificate change" (#1719)). This is a firewall-enforcement bypass: traffic that the administrator just disallowed keeps being forwarded/received for up to a full `firewall.conntrack_cache_timeout` interval, purely because the decision used the unsynchronized local cache instead of consulting the freshly bumped `rulesVersion`. This satisfies the "firewall bypass" impact category for this exercise.

### Likelihood Explanation
This does not require a malicious peer, a forged certificate, or any authentication bypass — it is triggered by the normal, documented reload flow (`reloadFirewall`) combined with ordinary continuing traffic from an already-established, legitimately-authenticated tunnel. Any deployment that relies on `firewall.conntrack_cache_timeout` for performance (a supported, documented tuning knob) and reloads its firewall rules to *tighten* access is exposed for the duration of that timeout. No attacker action beyond "keep sending packets on an existing tunnel" is needed, making this readily and repeatedly reachable in normal operation.

### Recommendation
Tie `ConntrackCache`/`ConntrackCacheTicker` invalidation to `f.rulesVersion` rather than (or in addition to) a wall-clock ticker, e.g. reset the local cache whenever a firewall reload occurs (bump a version the ticker also observes), or have `inConns`'s local-cache fast path also compare the last-seen `rulesVersion` recorded per cached tuple before skipping the authoritative recheck. This closes the same gap the M-4 report recommends for `close_pool_reward`: never let a cheap/lazy proxy substitute for a freshly-derived authoritative decision when a security-relevant action (allow/deny a packet, refund a pool) is at stake.

### Proof of Concept
1. Start Nebula with `firewall.conntrack_cache_timeout` set to a non-trivial value (e.g. 1s) and a rule allowing a given flow (matching the pattern of `TestFirewall_DropConntrackReload`, `firewall_test.go:667-729`).
2. Establish the tunnel and send steady traffic for that flow so it is repeatedly hitting `inConns` on the same reader goroutine, populating `localCache` (`firewall.go:573-576`).
3. While traffic is still flowing, reload the firewall config to add a stricter rule that would deny this flow's tuple (mirrors the existing `TestFirewall_DropConntrackReload` reload steps, `firewall_test.go:712-728`, but instead of calling `resetConntrack`/directly swapping the `Firewall`, perform it through the real `reloadFirewall` path so `f.rulesVersion` increments while the reader goroutine's `localCache` is untouched).
4. Observe that packets for the already-cached tuple continue to be forwarded (no `ErrNoMatchingRule`) until `ConntrackCacheTicker`'s tick fires and clears `localCache`, even though the authoritative `Conntrack.Conns` entry's `rulesVersion` mismatch would have dropped it had `localCache` not shadowed the check.

*(This proof-of-concept path was inferred from the existing `TestFirewall_DropConntrackReload` test and the `listenIn`/`ConntrackCacheTicker` wiring; verifying the exact timing window and reader-goroutine affinity requires running the described sequence, which was not executed as part of this repository search.)*

### Citations

**File:** firewall.go (L505-527)
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
```

**File:** firewall_test.go (L667-729)
```go
func TestFirewall_DropConntrackReload(t *testing.T) {
	ob := &bytes.Buffer{}
	l := test.NewLoggerWithOutput(ob)
	myVpnNetworksTable := new(bart.Lite)
	myVpnNetworksTable.Insert(netip.MustParsePrefix("1.1.1.1/8"))

	p := firewall.Packet{
		LocalAddr:  netip.MustParseAddr("1.2.3.4"),
		RemoteAddr: netip.MustParseAddr("1.2.3.4"),
		LocalPort:  10,
		RemotePort: 90,
		Protocol:   firewall.ProtoUDP,
		Fragment:   false,
	}
	network := netip.MustParsePrefix("1.2.3.4/24")

	c := cert.CachedCertificate{
		Certificate: &dummyCert{
			name:     "host1",
			networks: []netip.Prefix{network},
			groups:   []string{"default-group"},
			issuer:   "signer-shasum",
		},
		InvertedGroups: map[string]struct{}{"default-group": {}},
	}
	h := HostInfo{
		ConnectionState: &ConnectionState{
			peerCert: &c,
		},
		vpnAddrs: []netip.Addr{network.Addr()},
	}
	h.buildNetworks(myVpnNetworksTable, c.Certificate)

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

**File:** firewall/cache.go (L14-48)
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
