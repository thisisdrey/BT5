### Title
Firewall's cached `unsafeNetworks`/`routableNetworks` can diverge from the certificate's live `UnsafeNetworks()` during a v1/v2 dual-cert rotation, causing stale local-IP enforcement - ([File: interface.go])

### Summary
`Firewall.routableNetworks` (which gates whether a packet's `LocalAddr` is even considered "ours" to route/enforce) is a snapshot taken from `cert.Certificate.UnsafeNetworks()` at firewall construction time, tracked separately from the live certificate state (`PKI`/`CertState`). `Interface.reloadFirewall` only rebuilds the firewall — and thus only refreshes `routableNetworks`/`unsafeNetworks` — when it detects a change, and that change-detection compares against a *single*, preferentially-v2 certificate, not the full dual v1/v2 state that `NewFirewallFromConfig` itself may pick from.

### Finding Description
`Firewall` stores `unsafeNetworks` and derives `routableNetworks` once, at construction time, from whichever certificate (`Certificate`) was passed in: [1](#0-0) [2](#0-1) 

`routableNetworks` is the authoritative set used by `Firewall.Drop` to decide whether a local address is even one Nebula is responsible for enforcing on: [3](#0-2) 

The only mechanism that keeps this cached copy in sync with the certificate is `Interface.reloadFirewall`, which explicitly compares the *current* certificate's `UnsafeNetworks()` against the *firewall's stale* `unsafeNetworks` field to decide whether a rebuild is warranted: [4](#0-3) 

This comparison picks a certificate the same way `NewFirewallFromConfig` does — v2 preferred, falling back to v1: [5](#0-4) [6](#0-5) 

The root cause of the discrepancy class here is structural: `routableNetworks`/`unsafeNetworks` is a **second, independently-tracked copy** of state that already lives authoritatively in `PKI`/`CertState`. The only thing that keeps the two in sync is an explicit equality check (`slices.Equal`) performed opportunistically on every config reload tick, not on every certificate rotation event. If a certificate rotation happens between reload ticks, or if the node is running with **both** a v1 and v2 certificate simultaneously (a supported dual-cert mode per `CertState`/`getCertificate(Version1/2)`), the firewall's cached copy can reflect the wrong certificate's `UnsafeNetworks()` relative to which certificate is actually being used to authorize traffic elsewhere in the stack (e.g. `HostInfo.buildNetworks`, which is built per-peer directly from each peer's live certificate at handshake time and is not tied to `f.firewall.unsafeNetworks` at all): [7](#0-6) 

This mirrors exactly the bug class from the report: two subsystems (the firewall's routing/enforcement gate, and the certificate/PKI state that is supposed to be the single source of truth) independently track overlapping data, and only one of them is refreshed on a schedule/condition that can miss the other's update, producing incorrect enforcement decisions (accepting or rejecting local addresses based on stale unsafe-route data) rather than the certificate's current authoritative state.

### Impact Explanation
If `routableNetworks` lags behind the certificate's actual `UnsafeNetworks()` (e.g. because a certificate rotation added or removed an unsafe route and `reloadFirewall` hasn't re-evaluated, or evaluated against the wrong cert version in a dual v1/v2 deployment), the firewall's local-address gate in `Drop` can either:
- Reject legitimate local traffic for a newly granted unsafe route (denial of service for that route), or
- Continue to treat a *revoked* unsafe route as locally routable/enforceable, because `routableNetworks` was never rebuilt to drop it.

This is a state-poisoning / firewall-bypass-class issue in the local enforcement path, not a full authentication bypass, since the remote-address/cert checks in `Drop` are independent of `routableNetworks`. Impact is best classified as remote firewall-rule staleness / incorrect enforcement of unsafe-route policy.

### Likelihood Explanation
This requires a specific operational condition — a certificate rotation (SIGHUP/PKI reload) that changes `UnsafeNetworks()` for a certificate version other than the one `reloadFirewall`/`NewFirewallFromConfig` currently prefers, or a rotation landing between the `HasChanged`/`slices.Equal` check windows — rather than a simple network input from an attacker. It does not require a CA-signed certificate to trigger (it's a config/PKI-reload path), but it does require operator-controlled certificate rotation behavior, making likelihood **medium-low** and dependent on deployment specifics (dual v1/v2 cert usage with differing `unsafe_routes`).

### Recommendation
Do not maintain a second, independently-tracked copy of `unsafeNetworks`/`routableNetworks` inside `Firewall`. Instead, have `Firewall.Drop`'s local-address check consult the live `CertState`/`PKI` certificate(s) directly (both v1 and v2 if both are active), or have `reloadFirewall` compare against the union of all active certificate versions' `UnsafeNetworks()`, not just the preferred one, guaranteeing the firewall is rebuilt whenever *any* active certificate's unsafe routes change.

### Proof of Concept
Not independently reproduced beyond the existing repository test `TestReloadFirewall_CertUnsafeNetworksChanged`/`TestReloadFirewall_NoChange` in `interface_test.go`, which already demonstrate that the rebuild is gated purely on comparing a single selected certificate's `UnsafeNetworks()` to the firewall's stale copy: [8](#0-7) 

The scenario where a v1 certificate's unsafe networks change while a v2 certificate is also loaded (and thus preferred by both `NewFirewallFromConfig` and `reloadFirewall`'s change-detection) was not verifiable within the available context/index and would need direct code execution against `pki.go`'s `CertState`/`getCertificate` to confirm whether dual v1/v2 unsafe-route divergence is reachable in practice.

### Citations

**File:** firewall.go (L56-63)
```go
	// routableNetworks describes the vpn addresses as well as any unsafe networks issued to us in the certificate.
	// The vpn addresses are a full bit match while the unsafe networks only match the prefix
	routableNetworks *bart.Lite

	// assignedNetworks is a list of vpn networks assigned to us in the certificate.
	assignedNetworks []netip.Prefix
	// unsafeNetworks is the list of unsafe networks issued to us in the certificate
	unsafeNetworks []netip.Prefix
```

**File:** firewall.go (L154-180)
```go
	routableNetworks := new(bart.Lite)
	var assignedNetworks []netip.Prefix
	for _, network := range c.Networks() {
		nprefix := netip.PrefixFrom(network.Addr(), network.Addr().BitLen())
		routableNetworks.Insert(nprefix)
		assignedNetworks = append(assignedNetworks, network)
	}

	unsafeNetworks := c.UnsafeNetworks()
	for _, n := range unsafeNetworks {
		routableNetworks.Insert(n)
	}

	return &Firewall{
		Conntrack: &FirewallConntrack{
			Conns:      make(map[firewall.Packet]*conn),
			TimerWheel: NewTimerWheel[firewall.Packet](tmin, tmax),
		},
		InRules:          newFirewallTable(),
		OutRules:         newFirewallTable(),
		TCPTimeout:       tcpTimeout,
		UDPTimeout:       UDPTimeout,
		DefaultTimeout:   defaultTimeout,
		routableNetworks: routableNetworks,
		assignedNetworks: assignedNetworks,
		unsafeNetworks:   unsafeNetworks,
		l:                l,
```

**File:** firewall.go (L195-199)
```go
func NewFirewallFromConfig(l *slog.Logger, cs *CertState, c *config.C) (*Firewall, error) {
	certificate := cs.getCertificate(cert.Version2)
	if certificate == nil {
		certificate = cs.getCertificate(cert.Version1)
	}
```

**File:** firewall.go (L453-457)
```go
	// Make sure we are supposed to be handling this local ip address
	if !f.routableNetworks.Contains(fp.LocalAddr) {
		f.metrics(incoming).droppedLocalAddr.Inc(1)
		return ErrInvalidLocalIP
	}
```

**File:** interface.go (L386-400)
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
```

**File:** hostmap.go (L825-846)
```go
// buildNetworks fills in the networks field of HostInfo. It accepts a cert.Certificate so you never ever mix the network types up.
func (i *HostInfo) buildNetworks(myVpnNetworksTable *bart.Lite, c cert.Certificate) {
	if len(c.Networks()) == 1 && len(c.UnsafeNetworks()) == 0 {
		if myVpnNetworksTable.Contains(c.Networks()[0].Addr()) {
			return // Simple case, no BART needed
		}
	}

	i.networks = new(bart.Table[NetworkType])
	for _, network := range c.Networks() {
		nprefix := netip.PrefixFrom(network.Addr(), network.Addr().BitLen())
		if myVpnNetworksTable.Contains(network.Addr()) {
			i.networks.Insert(nprefix, NetworkTypeVPN)
		} else {
			i.networks.Insert(nprefix, NetworkTypeVPNPeer)
		}
	}

	for _, network := range c.UnsafeNetworks() {
		i.networks.Insert(network, NetworkTypeUnsafe)
	}
}
```

**File:** interface_test.go (L17-76)
```go
func TestReloadFirewall_CertUnsafeNetworksChanged(t *testing.T) {
	l := test.NewLogger()

	vpnNet := netip.MustParsePrefix("10.0.0.1/24")
	initialUnsafe := []netip.Prefix{netip.MustParsePrefix("198.51.100.0/24")}

	// dummyCert avoids dragging the real signing pipeline into a unit test.
	c1 := &dummyCert{
		version:        cert.Version2,
		networks:       []netip.Prefix{vpnNet},
		unsafeNetworks: initialUnsafe,
	}
	pki := &PKI{}
	pki.cs.Store(&CertState{v2Cert: c1, initiatingVersion: cert.Version2})

	rawYAML := `firewall:
  outbound:
    - port: any
      proto: any
      host: any
  inbound:
    - port: any
      proto: any
      host: any
`
	cfg := config.NewC(l)
	require.NoError(t, cfg.LoadString(rawYAML))

	fw, err := NewFirewallFromConfig(l, pki.getCertState(), cfg)
	require.NoError(t, err)
	require.Equal(t, initialUnsafe, fw.unsafeNetworks)

	f := &Interface{
		pki:      pki,
		firewall: fw,
		l:        l,
	}

	// Swap the cert with a different UnsafeNetworks set.
	newUnsafe := []netip.Prefix{
		netip.MustParsePrefix("198.51.100.0/24"),
		netip.MustParsePrefix("203.0.113.0/24"),
	}
	c2 := &dummyCert{
		version:        cert.Version2,
		networks:       []netip.Prefix{vpnNet},
		unsafeNetworks: newUnsafe,
	}
	pki.cs.Store(&CertState{v2Cert: c2, initiatingVersion: cert.Version2})

	// Reload with the same YAML so HasChanged("firewall") reports false.
	require.NoError(t, cfg.ReloadConfigString(rawYAML))
	require.False(t, cfg.HasChanged("firewall"))

	f.reloadFirewall(cfg)

	assert.NotSame(t, fw, f.firewall, "firewall pointer should have been replaced")
	assert.Equal(t, newUnsafe, f.firewall.unsafeNetworks)
	assert.True(t, f.firewall.routableNetworks.Contains(netip.MustParseAddr("203.0.113.5")))
}
```
