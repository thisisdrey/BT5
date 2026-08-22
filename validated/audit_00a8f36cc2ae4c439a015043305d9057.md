### Title
Firewall conntrack fast-path never revalidates established flows against a peer's rotated/downgraded certificate, allowing stale group-based authorization to persist - (File: firewall.go)

### Summary
The external report describes an ERC721 permit/operator address that keeps its transfer/approval rights after the underlying token ownership changes, because the contract never resets that stale authorization on transfer. The same bug class exists in nebula's firewall conntrack fast path: an already-established flow's permission is only re-validated when the **local firewall ruleset version** changes, never when the **remote peer's certificate** (and therefore its groups/permissions) changes via rehandshake. A peer whose certificate is reissued with reduced privileges (e.g. removed from a "trusted" group after key rotation or policy downgrade) keeps its already-permitted, actively-refreshed connections flowing indefinitely, exactly the way a stale ERC721 operator approval keeps working after the NFT changes hands.

### Finding Description
`Firewall.Drop` first checks the conntrack fast path via `inConns`, and only falls through to `table.match` (the actual group/CA rule evaluation against `h.ConnectionState.peerCert`) when there is no conntrack hit: [1](#0-0) 

The `conn` struct that backs each conntrack entry only records `Expires`, `incoming`, and `rulesVersion` — it never records anything about the peer identity/certificate that was checked when the flow was first admitted: [2](#0-1) 

`inConns` revalidates an existing conntrack entry against the current `table.match` **only** when `c.rulesVersion != f.rulesVersion`, i.e. only when the local firewall ruleset object has been reloaded/rebuilt. Otherwise it just extends `c.Expires` and returns `true` immediately, without ever consulting `h.ConnectionState.peerCert`: [3](#0-2) 

`rulesVersion` is only ever bumped from `reloadFirewall`, which fires on local `firewall` config changes or a change in the local certificate's `UnsafeNetworks`: [4](#0-3) 

Separately, when a *remote* peer rotates or downgrades its own certificate (e.g. losing group membership), nebula performs a rehandshake and updates `hostinfo.ConnectionState.peerCert` for future rule evaluations: [5](#0-4) 
but this rehandshake process, and connection-manager's rehandshake trigger `tryRehandshake`, never touch `Firewall.rulesVersion` or the `Conntrack` map at all: [6](#0-5) 

`connectionManager.isInvalidCertificate` only tears a tunnel down for expiry/blocklisting when `pki.disconnect_invalid` is enabled; a mere group change is not "invalid" and does not trigger any conntrack invalidation: [7](#0-6) 

Put together: a peer's certificate (its "credential"/ownership analog to the NFT owner) can be re-issued with reduced group membership, and the responder's firewall will rehandshake and pick up the new `peerCert` for *new* flows, but any flow that was already admitted under the old, more-permissive certificate remains conntracked and keeps being allowed — its "stale operator permission" (the previously-granted rule match) is never reset, exactly like the reported ERC721 issue where the operator's approval is never reset on transfer.

### Impact Explanation
This allows a peer whose certificate privileges have been reduced (revoked group, tightened firewall intent, compromised-key rotation with a lower-privilege reissue) to continue using previously-established connections indefinitely, as long as traffic keeps flowing to refresh the conntrack `Expires` timer. This is a concrete authorization-bypass/firewall-bypass: the local operator's intent (expressed via cert group downgrade) is silently ignored for existing sessions, letting the downgraded/former-trusted peer retain access to resources gated by group-based firewall rules that no longer apply to its current certificate.

### Likelihood Explanation
This is highly likely to be exploitable in practice: cert rotation with different groups is a first-class, documented nebula operational feature (used for revocation/least-privilege rotation), and TCP/UDP conntrack entries are refreshed on every packet, so a peer just needs to keep the flow alive (e.g. a long-lived TCP session or periodic traffic) across its own credential downgrade to retain the old permission with no additional attacker action required beyond normal traffic.

### Recommendation
Bind conntrack entries to the peer certificate/fingerprint (or a monotonically increasing "peer cert generation" counter) that was verified when the flow was admitted, and revalidate (or drop) any conntrack entry whenever the associated `HostInfo.ConnectionState.peerCert` changes (i.e. on every completed rehandshake), not only when the local `Firewall.rulesVersion` changes. Alternatively, force a full conntrack purge/re-check for a given hostinfo's flows whenever its peer certificate is replaced.

### Proof of Concept
1. Peer A holds a certificate in group `trusted` and establishes a long-lived TCP connection to Peer B, which has a firewall rule allowing `trusted` group inbound. The flow gets conntracked in `firewall.go`'s `Conntrack.Conns` map.
2. Operator revokes Peer A's `trusted` membership by issuing Peer A a new certificate without that group (standard credential rotation, no local firewall config reload on B).
3. Peer A rehandshakes with B (either forced by cert rotation on A's own SIGHUP or natural rehandshake); `hostinfo.ConnectionState.peerCert` on B is updated to the new, unprivileged certificate as shown in `handshake_manager.go` lines 870-879.
4. Peer A continues sending packets on the pre-existing TCP flow. On B, `Firewall.Drop` -> `inConns` finds the conntrack hit; since `f.rulesVersion` was never bumped (no local ruleset reload happened), `c.rulesVersion == f.rulesVersion`, so `table.match` is never re-invoked against the new, unprivileged `peerCert`, and the packet is allowed per `firewall.go` lines 505-578.
5. Peer A's existing session continues indefinitely despite no longer qualifying for the `trusted` group rule, while any *new* connection attempt from Peer A would correctly be rejected by `table.match` using the updated `peerCert`.

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

**File:** handshake_manager.go (L870-879)
```go
	// Handshake complete; build the ConnectionState now that we have keys and a verified peer cert.
	hostinfo.ConnectionState = newConnectionStateFromResult(result)

	remoteCert := result.RemoteCert
	if remoteCert == nil {
		f.l.Error("Handshake completed without peer certificate",
			"vpnAddrs", hostinfo.vpnAddrs, "from", via)
		hm.DeleteHostInfo(hostinfo)
		return
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

**File:** connection_manager.go (L502-550)
```go
func (cm *connectionManager) tryRehandshake(hostinfo *HostInfo) {
	cs := cm.intf.pki.getCertState()
	curCrt := hostinfo.ConnectionState.myCert
	curCrtVersion := curCrt.Version()
	myCrt := cs.getCertificate(curCrtVersion)
	if myCrt == nil {
		cm.l.Info("Re-handshaking with remote",
			"vpnAddrs", hostinfo.vpnAddrs,
			"version", curCrtVersion,
			"reason", "local certificate removed",
		)
		cm.intf.handshakeManager.StartHandshake(hostinfo.vpnAddrs[0], nil)
		return
	}
	peerCrt := hostinfo.ConnectionState.peerCert
	if peerCrt != nil && curCrtVersion < peerCrt.Certificate.Version() {
		// if our certificate version is less than theirs, and we have a matching version available, rehandshake?
		if cs.getCertificate(peerCrt.Certificate.Version()) != nil {
			cm.l.Info("Re-handshaking with remote",
				"vpnAddrs", hostinfo.vpnAddrs,
				"version", curCrtVersion,
				"peerVersion", peerCrt.Certificate.Version(),
				"reason", "local certificate version lower than peer, attempting to correct",
			)
			cm.intf.handshakeManager.StartHandshake(hostinfo.vpnAddrs[0], func(hh *HandshakeHostInfo) {
				hh.initiatingVersionOverride = peerCrt.Certificate.Version()
			})
			return
		}
	}
	if !bytes.Equal(curCrt.Signature(), myCrt.Signature()) {
		cm.l.Info("Re-handshaking with remote",
			"vpnAddrs", hostinfo.vpnAddrs,
			"reason", "local certificate is not current",
		)

		cm.intf.handshakeManager.StartHandshake(hostinfo.vpnAddrs[0], nil)
		return
	}
	if curCrtVersion < cs.initiatingVersion {
		cm.l.Info("Re-handshaking with remote",
			"vpnAddrs", hostinfo.vpnAddrs,
			"reason", "current cert version < pki.initiatingVersion",
		)

		cm.intf.handshakeManager.StartHandshake(hostinfo.vpnAddrs[0], nil)
		return
	}
}
```
