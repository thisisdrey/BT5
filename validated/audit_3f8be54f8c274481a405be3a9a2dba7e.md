## Title
Firewall conntrack entries are not re-validated against a peer's current certificate groups after cert renewal, allowing revoked group access to persist - (File: `firewall.go`)

### Summary
The bug report describes the `MixinTransfer.transferFrom`/`_setKeyManagerOf` bug class: a previously-granted authorization (`approved[tokenId]`) is not cleared when the underlying trust anchor (key manager / key ownership) changes, because the clearing logic is gated on a narrower condition than the one that actually needs re-validating. The reachable analog in nebula is the firewall conntrack cache: a connection is authorized once against a peer's certificate groups, and that authorization (`conn.rulesVersion`) is only re-checked when the **local** firewall ruleset version changes — never when the **peer's certificate** (and therefore its groups) changes. An attacker whose certificate groups are revoked (via cert renewal / re-issuance) but who keeps an already-authorized flow alive continues to be treated as authorized indefinitely.

### Finding Description
`Firewall.Drop` first checks `f.inConns` (the conntrack cache) before consulting the rule tables: [1](#0-0) 

`inConns` only re-validates a cached connection against the current rule table when the *firewall's* `rulesVersion` differs from the value stored on the conntrack entry: [2](#0-1) 

`rulesVersion` is only incremented in `reloadFirewall`, which fires on a local `firewall` config change or a change to the *local* certificate's `UnsafeNetworks`: [3](#0-2) 

Crucially, nothing increments `rulesVersion` (nor invalidates the specific conntrack entry) when the *peer's* certificate changes — e.g. when a peer renews its certificate with different (fewer/revoked) groups. `connectionManager.isInvalidCertificate` only tears down a tunnel for expiry or CA-blocklist reasons, not for a group/permission change in a still-valid cert: [4](#0-3) 

The conntrack cache key (`firewall.Packet`) is based on VPN address/port/proto tuples, not on the specific `HostInfo`/certificate instance, so even across a rehandshake that installs a fresh `peerCert` on the `HostInfo`, an already-cached flow for that same tuple keeps matching the old conntrack entry and is renewed (`c.Expires` extended) without ever being matched again against the new `h.ConnectionState.peerCert` — exactly the pattern in the report where `_setKeyManagerOf` skips clearing `approved` when its narrower precondition (`keyManagerOf[_tokenId] != _keyManager`) isn't met, letting a stale approval persist past the point where the underlying authority changed.

### Impact Explanation
A firewall rule that authorizes traffic based on certificate `group` membership (a common Nebula ACL pattern) can be silently bypassed for any already-established flow after the peer's authorizing group is revoked, as long as:
- the peer keeps sending/receiving on the same 5-tuple often enough to keep refreshing `conn.Expires`, and
- the local firewall config is not independently reloaded (which is the only thing that bumps `rulesVersion`).

`TCPTimeout` can be configured up to multi-day values (the code comment references "5 days max"), so a revoked peer can retain firewall-authorized access to that specific flow for a very long time after its authorizing certificate group was removed — a persistent, remote authorization-bypass condition reachable purely by a peer that already has *some* certificate signed by the trusted CA (no special privilege needed beyond normal handshake participation).

### Likelihood Explanation
This requires only: (1) a firewall rule keyed on certificate groups, (2) an already-open flow, and (3) that peer's certificate being renewed to drop a group without a corresponding local `firewall` config reload. Certificate renewal without a group is a normal operational event (e.g., decommissioning a user's access by re-issuing a narrower cert), so the precondition is realistic and does not require attacker-controlled exotic timing.

### Recommendation
Re-validate conntrack entries against the current `peerCert` whenever it changes, not only when the local `rulesVersion` changes — e.g., store a certificate fingerprint/version alongside each `conn` entry (similar to `rulesVersion`) and invalidate/re-match entries when the associated `HostInfo`'s `peerCert` fingerprint changes, mirroring how `_clearApproval` should be called unconditionally rather than being gated behind an unrelated check.

### Proof of Concept
1. Configure an inbound firewall rule permitting traffic only for certs in `group: contractors`.
2. Peer A holds a cert with `group: contractors`, establishes a tunnel, and opens a long-lived flow (e.g., a TCP connection) that is admitted via `Firewall.Drop` → `f.addConn`, creating a conntrack entry with the current `rulesVersion`.
3. Revoke A's access by issuing/rotating A's certificate to drop `contractors` (no change to the local `firewall.yaml`, so `rulesVersion` is unchanged).
4. A rehandshakes (new `peerCert` installed on `HostInfo`), but continues sending packets on the same 5-tuple.
5. `Firewall.Drop` → `f.inConns` finds the existing conntrack entry; since `c.rulesVersion == f.rulesVersion`, it skips `table.match` entirely and just refreshes `Expires`, allowing the traffic despite A no longer holding the `contractors` group — access persists until the flow naturally times out.

### Citations

**File:** firewall.go (L459-476)
```go
	// Check if we spoke to this tuple, if we did then allow this packet
	if f.inConns(fp, h, caPool, localCache) {
		return nil
	}

	table := f.OutRules
	if incoming {
		table = f.InRules
	}

	// We now know which firewall table to check against
	if !table.match(fp, incoming, h.ConnectionState.peerCert, caPool) {
		f.metrics(incoming).droppedNoRule.Inc(1)
		return ErrNoMatchingRule
	}

	// We always want to conntrack since it is a faster operation
	f.addConn(fp, incoming)
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

**File:** interface.go (L386-424)
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
```

**File:** connection_manager.go (L470-500)
```go
// isInvalidCertificate decides if we should destroy a tunnel.
// returns true if pki.disconnect_invalid is true and the certificate is no longer valid.
// Blocklisted certificates will skip the pki.disconnect_invalid check and return true.
func (cm *connectionManager) isInvalidCertificate(now time.Time, hostinfo *HostInfo) bool {
	remoteCert := hostinfo.GetCert()
	if remoteCert == nil {
		return false //don't tear down tunnels for handshakes in progress
	}

	caPool := cm.intf.pki.GetCAPool()
	err := caPool.VerifyCachedCertificate(now, remoteCert)
	if err == nil {
		return false //cert is still valid! yay!
	} else if err == cert.ErrBlockListed { //avoiding errors.Is for speed
		// Block listed certificates should always be disconnected
		hostinfo.logger(cm.l).Info("Remote certificate is blocked, tearing down the tunnel",
			"error", err,
			"fingerprint", remoteCert.Fingerprint,
		)
		return true
	} else if cm.intf.disconnectInvalid.Load() {
		hostinfo.logger(cm.l).Info("Remote certificate is no longer valid, tearing down the tunnel",
			"error", err,
			"fingerprint", remoteCert.Fingerprint,
		)
		return true
	} else {
		//if we reach here, the cert is no longer valid, but we're configured to keep tunnels from now-invalid certs open
		return false
	}
}
```
