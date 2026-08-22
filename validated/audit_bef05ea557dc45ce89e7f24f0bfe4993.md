## Title
Firewall conntrack cache is not re-validated against the peer's certificate identity/groups when a tunnel's certificate changes, allowing stale authorization to persist after cert rotation/revocation - (File: `firewall.go`)

### Summary
Nebula's firewall builds an "allow" decision (`Drop`/`inConns`) from the *current* peer certificate's name, groups, and issuer, then caches that decision in a conntrack table keyed only by the 5-tuple (`firewall.Packet`). The cached entry is later reused for all future packets in that flow without re-checking the peer certificate, unless the *firewall ruleset itself* changes (`f.rulesVersion`). If the identity behind the tunnel changes — e.g. the peer rehandshakes with a new/rotated/reduced-privilege certificate for the same vpn address, or the certificate is later blocklisted/expired — the previously cached conntrack "allow" keeps passing traffic that would no longer match the current rules, because the conntrack entry is never bound to, or re-validated against, the certificate that justified the original decision.

### Finding Description
The conntrack entry type only stores timing/direction/rule-version bookkeeping, with no reference to the certificate or fingerprint that produced the original allow decision: [1](#0-0) 

`Drop()` computes the firewall decision using `h.ConnectionState.peerCert` (the *live* certificate on the `HostInfo` at the time of the check) and, on success, calls `addConn`, which stores only `rulesVersion` and `incoming`/`Expires` — not the certificate/fingerprint that granted access: [2](#0-1) [3](#0-2) 

On subsequent packets, `inConns()` short-circuits the check and only re-runs the table match if the *firewall ruleset version* (`f.rulesVersion`) has changed — it never compares the certificate/identity that is now attached to the packet's `HostInfo` against the certificate that was used when the entry was created: [4](#0-3) 

Meanwhile, a certificate change on an existing tunnel is a normal, attacker-reachable event: `tryRehandshake`/cert reload swaps in a new certificate on the *same* hostinfo without touching the firewall's conntrack table at all: [5](#0-4) 

And `isInvalidCertificate` shows that Nebula explicitly anticipates certificates on an established tunnel becoming invalid or blocklisted later — but that only tears down the *tunnel*, it does not purge conntrack entries tied to that peer, and the conntrack cache has no notion of "this entry belonged to fingerprint X": [6](#0-5) 

This mirrors the Footium root cause: an authorization artifact (ERC20/721 approval in Footium; a conntrack "allow" decision in Nebula) is granted based on a specific identity/owner context, but persists and continues to be honored after that identity/ownership context changes, because the artifact is not scoped to (or invalidated on a change of) the identity that created it — only an unrelated event (club "sale" completing in Footium; a firewall config reload bumping `rulesVersion` in Nebula) is checked for invalidation.

### Impact Explanation
A peer whose certificate is rotated to a smaller set of groups/name (e.g., an admin demotes a compromised or departing host to a restricted group, or blocklists/expires its certificate) can continue to send/receive packets on any flow tuple that was already conntracked under the old, more privileged certificate, until that specific conntrack entry naturally expires (`TCPTimeout`/`UDPTimeout`/`DefaultTimeout`, which can be days for TCP) or an unrelated firewall reload occurs. This is a firewall bypass: traffic that should be blocked under the peer's current certificate is allowed because the authorization was cached against a stale identity.

### Likelihood Explanation
This requires no attacker-forged certificate — it is triggered by the normal, expected lifecycle of a legitimately-signed certificate being rotated/downgraded/blocklisted for a host that already has an established, conntracked flow, which is a supported, encouraged operational pattern (`pki.disconnect_invalid`, cert rotation on SIGHUP/reload, blocklisting). No malicious peer/lighthouse cooperation is needed beyond the normal handshake flow already in scope.

### Recommendation
Bind conntrack entries to the certificate fingerprint (or a monotonically increasing per-HostInfo cert-version) that authorized them, and re-validate (or drop) entries whenever the associated `HostInfo.ConnectionState.peerCert` changes — not only when the global firewall `rulesVersion` changes. Alternatively, purge/re-validate all conntrack entries for a `HostInfo` whenever its certificate is replaced via rehandshake, or whenever `isInvalidCertificate`/blocklisting logic detects the peer's certificate is no longer valid.

### Proof of Concept
1. Host A and Host B establish a tunnel; Host A is issued a certificate with `groups: ["full-access"]`, matching an inbound firewall rule.
2. Host A sends a packet, `Firewall.Drop` matches on `full-access` and caches the flow in `Conntrack.Conns` via `addConn` (only `rulesVersion` stored, no cert reference). [7](#0-6) 
3. The operator rotates Host A's certificate to `groups: ["restricted"]` (no rule change, so `f.rulesVersion` is unchanged) and Host A rehandshakes; `hostinfo.ConnectionState` is replaced with the new cert. [8](#0-7) 
4. Host A continues sending packets on the same 5-tuple. `inConns()` finds the existing conntrack entry, sees `c.rulesVersion == f.rulesVersion`, and allows the packet without re-checking that the *new* `restricted` certificate still satisfies the `full-access` rule. [9](#0-8) 
5. Traffic that should now be blocked under Host A's demoted certificate continues to pass until the conntrack entry's `Expires` timeout elapses or an unrelated firewall reload bumps `rulesVersion`.

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

**File:** connection_manager.go (L502-540)
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
```

**File:** handshake_manager.go (L870-871)
```go
	// Handshake complete; build the ConnectionState now that we have keys and a verified peer cert.
	hostinfo.ConnectionState = newConnectionStateFromResult(result)
```
