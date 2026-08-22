### Title
Data-plane packet decryption never re-validates peer certificate expiry, decoupling live traffic authorization from certificate-validity enforcement - (File: connection_state.go)

### Summary
Nebula's tunnel data path authorizes every inbound packet purely on possession of a valid Noise session (successful AEAD decrypt) and anti-replay window state, `cs.window.Check`/`cs.window.Update`, in `ConnectionState.Decrypt`. Certificate expiry / CA-trust revalidation happens on a completely separate, periodically-ticked code path in the connection manager. This mirrors the reported DeFi bug class: the mechanism that continuously grants trust ("is this tunnel still allowed to pass traffic") and the mechanism that is supposed to revoke it ("is the peer's certificate still valid") operate on different time bases and different data, creating an exploitable window where traffic keeps flowing on a certificate that should no longer be trusted. [1](#0-0) 

### Finding Description
Every inbound message packet goes through `Interface.readOutsidePackets` → `hostinfo.ConnectionState.Decrypt`, which only checks the anti-replay `Bits` window before/after AEAD decryption — there is no certificate expiry or CA-pool re-verification in this hot path: [1](#0-0) [2](#0-1) 

Certificate validity is instead checked only by `connectionManager.isInvalidCertificate`, which is invoked from `makeTrafficDecision`, itself driven by a lazily-ticked `trafficTimer`/`checkInterval` schedule rather than per packet: [3](#0-2) [4](#0-3) 

Crucially, this enforcement is gated behind `pki.disconnect_invalid`, which historically defaults to `false`: [5](#0-4) 

So when `disconnect_invalid` is left at its default, `isInvalidCertificate` computes that the certificate is expired/blocklisted-except-blocklist but explicitly chooses to keep the tunnel alive: [6](#0-5) 

This is structurally the same flaw as the report: the "health" check (data-plane packet acceptance, which is what actually matters — it moves real traffic) uses one criterion (replay window only, checked continuously and immediately) while the "enforcement" check (certificate-based trust revocation, the equivalent of slashing) uses a different, lagging, and optionally-disabled time basis. A certificate that has expired, or whose CA has been rotated/revoked, does not stop authorizing decrypted traffic; only an out-of-band, periodic, and optional tick can tear the tunnel down, and by default it never does.

### Impact Explanation
An already-established Noise session continues to decrypt and authorize application traffic indefinitely past the peer certificate's `NotAfter`, or after the certificate is otherwise no longer trusted by the local CA pool, as long as `pki.disconnect_invalid` (default `false`) is not explicitly enabled. This is a concrete authentication-bypass/certificate-verification bypass at the data-plane level: the identity assurance that the mesh depends on (a currently valid certificate) is silently detached from the mechanism that actually forwards decrypted traffic in and out of the tunnel.

### Likelihood Explanation
No attacker action beyond normal operation is required — the certificate simply needs to expire (or its CA trust needs to lapse) while a session is already established, which is a routine and inevitable event in any long-lived Nebula deployment. Because the default configuration does not enable `disconnect_invalid`, this is the out-of-the-box behavior, not an edge case requiring misconfiguration by an attacker.

### Recommendation
- Short term: have the data-plane path (`ConnectionState.Decrypt` / `readOutsidePackets`) consult certificate validity on the same cadence packets are processed, or at minimum shrink the enforcement interval and make `pki.disconnect_invalid` effectively mandatory (or default `true`), so tunnels are torn down promptly and consistently once the peer certificate is no longer trusted.
- Long term: unify the trust state used for continuous traffic authorization with the trust state used for tunnel teardown, so a single, real-time-consistent check governs both — analogous to requiring collateral used for solvency/health calculations to always match what can actually be slashed.

### Proof of Concept
1. Establish a Nebula tunnel between two nodes using a peer certificate with a short `NotAfter`.
2. Leave `pki.disconnect_invalid` unset (default `false`).
3. Let the certificate expire while the tunnel remains active (send periodic traffic so `connectionManager` sees inbound traffic and does not drop it for inactivity).
4. Observe via `connectionManager.isInvalidCertificate` (or logs) that the cert is recognized as expired, but per `connection_manager.go:490-499`, traffic keeps flowing because the tunnel is not torn down.
5. Continue sending encrypted data-plane packets; `ConnectionState.Decrypt` (`connection_state.go:61-82`) keeps accepting/decrypting them since it never inspects certificate expiry, confirming traffic is authorized indefinitely on an invalid certificate.

### Citations

**File:** connection_state.go (L61-82)
```go
func (cs *ConnectionState) Decrypt(l *slog.Logger, messageCounter uint64, out []byte, packet []byte, nb []byte) ([]byte, error) {
	var err error
	cs.decryptLock.Lock()
	result := cs.window.Check(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}

	out, err = cs.dKey.DecryptDanger(out, packet[:header.Len], packet[header.Len:], messageCounter, nb)
	if err != nil {
		return nil, err
	}

	cs.decryptLock.Lock()
	result = cs.window.Update(l, messageCounter)
	cs.decryptLock.Unlock()
	if !result {
		return nil, ErrAlreadySeen
	}
	return out, nil
}
```

**File:** outside.go (L126-136)
```go
	out, err = hostinfo.ConnectionState.Decrypt(f.l, h.MessageCounter, out, packet, nb)
	if err != nil {
		if f.l.Enabled(context.Background(), slog.LevelDebug) {
			hostinfo.logger(f.l).Debug("Failed to decrypt packet", "error", err, "from", via, "header", h)
		}
		return
	}

	// Roam before we respond
	f.handleHostRoaming(hostinfo, via)
	f.connectionManager.In(hostinfo)
```

**File:** connection_manager.go (L311-324)
```go
func (cm *connectionManager) makeTrafficDecision(localIndex uint32, now time.Time) (trafficDecision, *HostInfo, *HostInfo) {
	// Read lock the main hostmap to order decisions based on tunnels being the primary tunnel
	cm.hostMap.RLock()
	defer cm.hostMap.RUnlock()

	hostinfo := cm.hostMap.Indexes[localIndex]
	if hostinfo == nil {
		cm.l.Debug("Not found in hostmap", "localIndex", localIndex)
		return doNothing, nil, nil
	}

	if cm.isInvalidCertificate(now, hostinfo) {
		return closeTunnel, hostinfo, nil
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

**File:** CHANGELOG.md (L584-586)
```markdown
- New config option `pki.disconnect_invalid` that will tear down tunnels when they become invalid (through expiry or
  removal of root trust). Default is `false`. Note, this will not currently recognize if a remote has changed
  certificates since the last handshake. (#370)
```
