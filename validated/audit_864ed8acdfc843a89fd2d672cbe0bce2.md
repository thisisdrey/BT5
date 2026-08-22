### Title
Data-Plane Firewall Never Re-Checks Certificate Validity/Revocation, Allowing Continued Traffic on Blocklisted or Expired Peer Certificates - (File: `firewall.go`)

### Summary
This is an analog of the reported DeFi bug class: a resource ("bucket"/tunnel) that has been "delisted" (blocklisted/expired/revoked) can still be used to transact ("increase debt"/pass traffic) because the enforcement point (`increaseDebt()`/`Firewall.Drop()`) does not itself validate the delisted status — that validation lives elsewhere and is either optional or only periodic.

### Finding Description
`Firewall.Drop()` is the sole per-packet authorization gate for both inbound and outbound overlay traffic [1](#0-0) . It validates remote/local address membership, conntrack state, and firewall rule matching, but it never calls into the CA pool to verify that the peer's certificate (`h.ConnectionState.peerCert`) is still valid — i.e. not expired and not blocklisted — before allowing the packet: [2](#0-1) . The `caPool` parameter passed into `Drop()`/`table.match()`/`inConns()` is used only to resolve CA name/SHA firewall-rule predicates, never to call `VerifyCertificate`/`VerifyCachedCertificate`/`IsBlocklisted` [3](#0-2) .

Certificate validity is instead enforced only in two other places:
1. At handshake time, via `certVerifier()` calling `CAPool.VerifyCertificate` [4](#0-3) .
2. Periodically (on the connection-manager's traffic-check timer) via `isInvalidCertificate()`, which calls `VerifyCachedCertificate` [5](#0-4) .

Critically, in `isInvalidCertificate()`, a blocklisted certificate always tears down the tunnel, but a merely-expired (no-longer-valid) certificate only tears down the tunnel if `pki.disconnect_invalid` is explicitly enabled by the operator; otherwise the tunnel — and therefore the firewall's implicit "trust" of that peer — is deliberately kept alive [6](#0-5) . This is confirmed by the test `Test_NewConnectionManagerTest_DisconnectInvalid`, which must explicitly `ifce.disconnectInvalid.Store(true)` for expired-cert teardown to occur [7](#0-6) .

This mirrors the report's root cause exactly: the authoritative "is this entity still allowed" check (`primexDNS.getBucketAddress()` / bucket "isActive") is enforced only at creation/entry points (handshake) and at a separate, best-effort periodic sweep (connection manager timer), while the actual value-transfer/debt-increasing operation (`Bucket.increaseDebt()` / here, `Firewall.Drop()` on the data plane) has no explicit, local revalidation of the underlying trust object's status.

### Impact Explanation
If an operator blocklists a certificate fingerprint via `pki.blocklist` (the nebula equivalent of "delisting"), an already-established tunnel to that peer keeps passing data-plane traffic through `Firewall.Drop()` until the next connection-manager tick fires `isInvalidCertificate()` and tears the tunnel down — this is a window of continued unauthorized traffic. Worse, for certificate expiration (as opposed to explicit blocklisting), continued traffic flow depends entirely on the `pki.disconnect_invalid` setting; if unset/false, expired-certificate peers keep talking to the firewall indefinitely with no data-plane check ever rejecting their packets on the basis of certificate validity. This is a remote-state-poisoning / authorization-bypass class issue: the firewall grants ongoing traffic authorization to a peer identity whose credential has been revoked or expired, based purely on a stale in-memory `HostInfo`/conntrack state.

### Likelihood Explanation
This is reachable by any already-handshaked peer (no CA-signed valid certificate is required at the moment of exploitation — that's exactly the point: the certificate becomes invalid/blocklisted *after* the tunnel was established) with no additional privilege. The bug is deterministic and not dependent on timing races beyond the normal connection-manager tick interval, and the `disconnect_invalid` default-off behavior for expired certs is a persistent, not transient, gap.

### Recommendation
Add an explicit certificate-validity check inside `Firewall.Drop()` (or immediately before it is invoked in `consumeInsidePacket`/`handleOutsideMessagePacket`), calling `caPool.VerifyCachedCertificate(now, h.ConnectionState.peerCert)` and rejecting the packet (with a distinct drop reason, e.g. `ErrCertificateInvalid`) if it fails — mirroring the recommendation to consolidate the "not delisted" check inside the state-mutating function itself (`Bucket.increaseDebt()`) rather than relying solely on upstream/periodic validation. Additionally, reconsider making `pki.disconnect_invalid` behavior (or at minimum blocklist enforcement) apply immediately at the packet-processing layer rather than only at the next connection-manager tick.

### Proof of Concept
1. Establish a tunnel between host A and host B with valid certificates (handshake succeeds, `HostInfo.ConnectionState.peerCert` cached).
2. Operator blocklists B's certificate fingerprint via `pki.blocklist` reload, or B's certificate's `NotAfter` time passes (`Expired`).
3. Before the connection manager's next `makeTrafficDecision`/`isInvalidCertificate` tick runs (or indefinitely if `pki.disconnect_invalid` is not set for the expiry case), B continues sending/receiving data-plane packets; each packet passes through `Firewall.Drop()` at `firewall.go:425-479`, which never calls `caPool.VerifyCachedCertificate` and therefore allows the traffic as long as it matches a rule/conntrack entry [8](#0-7) .
4. Traffic keeps flowing despite the peer's certificate being revoked/expired, until the periodic connection-manager check (and, for expiry, only if `disconnect_invalid: true`) eventually tears down the tunnel.

### Citations

**File:** firewall.go (L423-479)
```go
// Drop returns an error if the packet should be dropped, explaining why. It
// returns nil if the packet should not be dropped.
func (f *Firewall) Drop(fp firewall.Packet, incoming bool, h *HostInfo, caPool *cert.CAPool, localCache firewall.ConntrackCache) error {
	// Make sure remote address matches nebula certificate, and determine how to treat it
	if h.networks == nil {
		// Simple case: Certificate has one address and no unsafe networks
		if h.vpnAddrs[0] != fp.RemoteAddr {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
	} else {
		nwType, ok := h.networks.Lookup(fp.RemoteAddr)
		if !ok {
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrInvalidRemoteIP
		}
		switch nwType {
		case NetworkTypeVPN:
			break // nothing special
		case NetworkTypeVPNPeer:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrPeerRejected // reject for now, one day this may have different FW rules
		case NetworkTypeUnsafe:
			break // nothing special, one day this may have different FW rules
		default:
			f.metrics(incoming).droppedRemoteAddr.Inc(1)
			return ErrUnknownNetworkType //should never happen
		}
	}

	// Make sure we are supposed to be handling this local ip address
	if !f.routableNetworks.Contains(fp.LocalAddr) {
		f.metrics(incoming).droppedLocalAddr.Inc(1)
		return ErrInvalidLocalIP
	}

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

**File:** firewall.go (L505-536)
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
```

**File:** handshake_manager.go (L1161-1166)
```go
// certVerifier returns a CertVerifier that validates certs against the current CA pool.
func (hm *HandshakeManager) certVerifier() handshake.CertVerifier {
	return func(c cert.Certificate) (*cert.CachedCertificate, error) {
		return hm.f.pki.GetCAPool().VerifyCertificate(time.Now(), c)
	}
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

**File:** connection_manager_test.go (L356-388)
```go
	ifce.pki.cs.Store(cs)
	ifce.pki.caPool.Store(ncp)
	ifce.disconnectInvalid.Store(true)

	// Create manager
	conf := config.NewC(test.NewLogger())
	punchy := NewPunchyFromConfig(test.NewLogger(), conf, nil)
	nc := newConnectionManagerFromConfig(test.NewLogger(), conf, hostMap, punchy)
	nc.intf = ifce
	ifce.connectionManager = nc

	hostinfo := &HostInfo{
		vpnAddrs: []netip.Addr{vpnIp},
		ConnectionState: &ConnectionState{
			myCert:   &dummyCert{},
			peerCert: cachedPeerCert,
		},
	}
	nc.hostMap.unlockedAddHostInfo(hostinfo, ifce)

	// Move ahead 45s.
	// Check if to disconnect with invalid certificate.
	// Should be alive.
	nextTick := now.Add(45 * time.Second)
	invalid := nc.isInvalidCertificate(nextTick, hostinfo)
	assert.False(t, invalid)

	// Move ahead 61s.
	// Check if to disconnect with invalid certificate.
	// Should be disconnected.
	nextTick = now.Add(61 * time.Second)
	invalid = nc.isInvalidCertificate(nextTick, hostinfo)
	assert.True(t, invalid)
```
