### Title
Firewall conntrack table grants continued traffic passage without revalidating against a peer's re-issued/re-authenticated certificate - (File: firewall.go)

### Summary
Nebula's stateful firewall records an "approval" for a flow tuple in the `Conntrack.Conns` map the first time it passes a rule match, and subsequent packets for that tuple bypass rule evaluation until the entry expires or the *firewall ruleset itself* changes (`rulesVersion`). The approval is keyed purely on the packet 4-tuple (`firewall.Packet`), never on the specific certificate/HostInfo that earned it, and is only re-validated against the current firewall table when `f.rulesVersion` changes — never when the remote peer's certificate changes (e.g. a rehandshake that renews or narrows the peer's certificate, or a certificate transitioning toward invalidity). This mirrors the reported Vault bug class: a one-time authorization ("approval"/conntrack grant) is not reset when the context that justified it changes, so it can be leveraged after the point where the original permission should no longer apply.

### Finding Description
`Firewall.Drop` first checks the source/destination against the current `HostInfo`'s certificate-derived networks, then checks `f.inConns` (the conntrack cache) before consulting `OutRules`/`InRules`: [1](#0-0) 

`inConns` only re-validates a cached tuple against the current rule tables when the entry's `rulesVersion` differs from the firewall's current `rulesVersion` (which increments only on a config/`firewall` block reload or a certificate `UnsafeNetworks()` change): [2](#0-1) 

The conntrack entry itself stores only `Expires`, `incoming`, and `rulesVersion` — no binding to the certificate, fingerprint, or groups that were checked when the rule initially matched: [3](#0-2) 

`addConn` records the approval keyed solely by the packet tuple, again with no certificate binding: [4](#0-3) 

The `Firewall` object and its `Conntrack` table are attributes of the `Interface`, not of a `HostInfo`, and they are explicitly *carried over* across a firewall rebuild unless the rebuild is itself a config/cert-UnsafeNetworks change: [5](#0-4) 

Firewall rules can match on group membership or CA signer hash (`caName`/`caSha`), as exercised by `TestFirewall_Drop3`/`TestFirewall_Drop`. Because rule matches for groups/signer are only re-evaluated when `f.rulesVersion` changes, a peer who legitimately earns a conntrack grant under one certificate (e.g. member of `"default-group"`) and later re-handshakes with a **different** certificate that would no longer pass the rule table (revoked group membership, different signer, or a certificate that has simply expired/become invalid per `isInvalidCertificate`) continues to have its already-approved flow tuple pass the firewall for the remainder of the conntrack TTL (`TCPTimeout` up to 5 days by kernel default reference in comments, `UDPTimeout`, `DefaultTimeout`) — the code path that revalidates conntrack entries never fires because no firewall config change occurred: [6](#0-5) 

Certificate invalidity itself is only enforced at the connection-manager tick level (tearing down the whole tunnel), not by invalidating conntrack state for surviving flows tied to the same 4-tuple if a new tunnel is established at the same overlay address before the connection manager acts: [7](#0-6) 

### Impact Explanation
This is a remote state-poisoning / firewall-bypass analog: a legitimately-connected peer whose certificate is later downgraded, whose group membership is revoked, or who rotates to a certificate that a firewall rule (host/group/CA-signer based) would now reject, can keep an already-approved flow alive through the stale conntrack "approval" — exactly the class of bug described in the report, where a one-time approval is not reset once the authorization context that produced it is no longer valid. This can permit continued authorized-looking traffic to reach a workload behind rules that were supposed to have re-scoped or cut off that peer, without the peer needing any CA-signed certificate change performed by the defender-side operator to be noticed (the firewall only re-checks on its own ruleset version bump, not on peer identity changes).

### Likelihood Explanation
Exploitation requires a peer that already has a valid nebula certificate and an established tunnel (not an unauthenticated outsider), and depends on the specific sequence: initial flow approval, then a change to the peer's certificate/group/signer status that a currently-loaded firewall ruleset would reject, occurring without a corresponding firewall config reload. This is a narrower, certificate-holding-peer scenario, so likelihood is lower than a fully unauthenticated bypass, but it is realistic in long-lived deployments using group- or CA-based firewall rules combined with certificate rotation, and it requires no attacker action beyond normal handshake/rehandshake behavior.

### Recommendation
- Bind conntrack entries to the fingerprint (or a hash of group/CA-relevant certificate attributes) of the certificate that was active when the entry was created, and re-validate/evict entries whenever the associated `HostInfo`'s certificate changes (rehandshake, cert reload) — not only when `f.rulesVersion` changes.
- Alternatively, proactively purge/re-validate conntrack entries for a `HostInfo` whenever its `ConnectionState`/certificate is replaced (e.g., in `continueHandshake`'s "handshake complete" path and in `connection_manager.isInvalidCertificate`), similar to how `reloadFirewall` already purges/re-validates on ruleset changes.
- Long term: audit every place a nebula grant (conntrack, relay `Established` state, cached certificate verification results) outlives the specific authorization context that created it, and ensure a state transition on the authorizing side (cert rotation, revocation, group change) forces re-validation of dependent state rather than relying on it lazily aging out via TTL/version counters that are unrelated to the peer's identity.

### Proof of Concept
Conceptual sequence (not confirmed by a runnable exploit in the index, since no test in-scope demonstrates group/CA-rule downgrade across a live conntrack entry):
1. Peer P completes a handshake with certificate C1, member of group `"trusted"`; firewall rule allows `"trusted"` group only.
2. P sends traffic on tuple `fp`; `Firewall.Drop` matches the rule and calls `addConn(fp, ...)`, storing an approval tied only to `fp` and the *current* `f.rulesVersion` [8](#0-7) .
3. P rehandshakes (or its operator revokes/rotates its certificate to drop the `"trusted"` group) without any nebula firewall config reload occurring on this node, so `f.rulesVersion` is unchanged.
4. P continues sending on the same tuple `fp`; `Firewall.Drop` calls `f.inConns`, which finds the cached entry, sees `c.rulesVersion == f.rulesVersion`, and returns `true` without ever re-matching against `OutRules`/`InRules` using the new (non-`"trusted"`) certificate [9](#0-8) .
5. Traffic that should now be rejected under the current ruleset for P's new certificate continues to flow until the conntrack entry naturally expires per `TCPTimeout`/`UDPTimeout`/`DefaultTimeout`.

This proof-of-concept sequence is inferred directly from the code paths cited above; I could not find an existing e2e/unit test in the indexed codebase that exercises a live certificate/group change against an already-approved conntrack entry, so I cannot confirm with full certainty whether some other layer (e.g. connection manager's periodic invalid-certificate teardown, which tears down the whole tunnel rather than the conntrack entry) narrows the exploit window in practice. A Devin session with full repository/test access would be needed to write and run a concrete reproduction confirming the exact window of exposure.

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

**File:** firewall.go (L50-58)
```go
	//TODO: we should have many more options for TCP, an option for ICMP, and mimic the kernel a bit better
	// https://www.kernel.org/doc/Documentation/networking/nf_conntrack-sysctl.txt
	TCPTimeout     time.Duration //linux: 5 days max
	UDPTimeout     time.Duration //linux: 180s max
	DefaultTimeout time.Duration //linux: 600s

	// routableNetworks describes the vpn addresses as well as any unsafe networks issued to us in the certificate.
	// The vpn addresses are a full bit match while the unsafe networks only match the prefix
	routableNetworks *bart.Lite
```

**File:** firewall.go (L459-479)
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

	return nil
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
