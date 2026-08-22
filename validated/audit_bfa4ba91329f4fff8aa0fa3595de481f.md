### Title
Non-atomic PKI reload creates a certificate/CA-pool trust window during config reload - ([File: pki.go])

### Summary
Nebula's `PKI.reload` updates the host's certificate state and the CA trust pool as two independent, non-atomic steps. `reloadCerts` calls `p.cs.Store(newState)` and `reloadCAPool` calls `p.caPool.Store(caPool)` in sequence, with no synchronization tying the two updates together. This mirrors the reported bug class of splitting two operations that must be atomic (init + supply) into separate transactions/steps, leaving a window where the system is in an inconsistent, exploitable intermediate state.

### Finding Description
`PKI.reload` performs:
```go
func (p *PKI) reload(c *config.C, initial bool) error {
	err := p.reloadCerts(c, initial)
	...
	err = p.reloadCAPool(c)
	...
}
``` [1](#0-0) 

`reloadCerts` independently replaces the atomic pointer holding the host's own certificate/credential state: [2](#0-1) 

`reloadCAPool` independently replaces the atomic pointer holding the trusted CA pool (including any blocklist entries): [3](#0-2) 

Because these are two separate atomic-pointer swaps rather than one atomic transition, concurrent handshake processing (`beginHandshake` / `continueHandshake`) that reads `f.pki.getCertState()` and `f.pki.GetCAPool()` at different points can observe a partially-updated PKI state: e.g., the new certificate state has been installed but the CA pool that should invalidate/blocklist a peer's certificate as part of the same config change has not yet been swapped in (or vice versa). During this window, a peer whose trust is being revoked (CA rotation or blocklist update delivered in the same reload) can still complete handshake authentication and certificate verification against the stale CA pool, exactly as the original report describes an attacker exploiting the window between two steps that should have been a single atomic operation. [4](#0-3) [5](#0-4) 

### Impact Explanation
If a network operator rotates a CA or updates the blocklist together with a certificate change (common operational pattern, e.g., during a compromise response), a remote peer holding a certificate that is being revoked can race the reload and complete or maintain a handshake using the stale, still-loaded `CAPool`, effectively bypassing the intended certificate/CA trust update. This is a genuine authentication/trust-verification bypass window reachable purely over the network during a config reload event, without any change to the attacker's own capabilities.

### Likelihood Explanation
Likelihood is low: it requires (a) an operator to trigger a config reload that changes both cert state and CA pool/blocklist together, and (b) an attacker to have a handshake in flight or race the exact window between the two `Store` calls. This matches the "High impact / Low likelihood" characterization of the original report — the window is narrow but the consequence (temporarily trusting a revoked identity) is severe.

### Recommendation
Combine the certificate-state and CA-pool reload into a single atomic update (e.g., store both under one atomic pointer to a composite struct, or take a lock across both `Store` calls) so that handshake/verification code never observes a state where one has been updated and the other has not. This mirrors the original recommendation to perform both the initialization and follow-up step in the same atomic operation rather than as separate, independently observable steps.

### Proof of Concept
1. Configure Nebula with `pki.ca` trusting CA-A and a certificate signed by CA-A for a peer.
2. Trigger a config reload that simultaneously rotates `pki.cert`/`pki.key` to a new identity and removes CA-A from `pki.ca` (or adds the peer's fingerprint to `pki.blocklist`).
3. Have the peer initiate/continue a handshake concurrently with the reload.
4. Because `reloadCerts` (`p.cs.Store`) and `reloadCAPool` (`p.caPool.Store`) execute as separate steps [1](#0-0) , a handshake processed between the two stores can be validated against the old `CAPool` still containing CA-A, allowing the revoked peer to complete authentication (`validatePeerCert` → `hm.CheckAndComplete`) despite the intended revocation. [6](#0-5)

### Citations

**File:** pki.go (L77-95)
```go
func (p *PKI) reload(c *config.C, initial bool) error {
	err := p.reloadCerts(c, initial)
	if err != nil {
		if initial {
			return err
		}
		err.Log(p.l)
	}

	err = p.reloadCAPool(c)
	if err != nil {
		if initial {
			return err
		}
		err.Log(p.l)
	}

	return nil
}
```

**File:** pki.go (L186-193)
```go
	p.cs.Store(newState)

	if initial {
		p.l.Debug("Client nebula certificate(s)", "cert", newState)
	} else {
		p.l.Info("Client certificate(s) refreshed from disk", "cert", newState)
	}
	return nil
```

**File:** pki.go (L196-205)
```go
func (p *PKI) reloadCAPool(c *config.C) *util.ContextualError {
	caPool, err := loadCAPoolFromConfig(p.l, c)
	if err != nil {
		return util.NewContextualError("Failed to load ca from config", nil, err)
	}

	p.caPool.Store(caPool)
	p.l.Debug("Trusted CA fingerprints", "fingerprints", caPool.GetFingerprints())
	return nil
}
```

**File:** handshake_manager.go (L701-750)
```go
func (hm *HandshakeManager) beginHandshake(via ViaSender, packet []byte, h *header.H) {
	f := hm.f
	cs := f.pki.getCertState()

	v := cs.DefaultVersion()
	if cs.GetCredential(v) == nil {
		f.l.Error("Unable to handshake with host because no certificate is available",
			"from", via, "certVersion", v)
		return
	}

	machine, err := handshake.NewMachine(
		v, cs.GetCredential,
		hm.certVerifier(), func() (uint32, error) { return generateIndex(f.l) },
		false, header.HandshakeIXPSK0,
	)
	if err != nil {
		f.l.Error("Failed to create handshake machine", "from", via, "error", err)
		return
	}

	response, result, err := machine.ProcessPacket(nil, packet)
	if err != nil {
		f.l.Error("Failed to process handshake packet", "from", via, "error", err)
		return
	}

	if result == nil {
		// Multi-message pattern: the responder Machine would need to be
		// registered in hm.indexes so a future inbound packet finds it via
		// continueHandshake. The current manager doesn't do that yet, so
		// fail loudly rather than silently dropping the in-flight handshake.
		// TODO: support multi-message responder flows (XX, pqIX, etc.).
		// See also the IX-shaped cipher key assignment in handshake.Machine.
		f.l.Error("multi-message handshake responder is not supported",
			"from", via, "error", handshake.ErrMultiMessageUnsupported)
		return
	}

	remoteCert := result.RemoteCert
	if remoteCert == nil {
		f.l.Error("Handshake did not produce a peer certificate", "from", via)
		return
	}

	// Validate peer identity
	vpnAddrs, anyVpnAddrsInCommon, ok := hm.validatePeerCert(via, remoteCert)
	if !ok {
		return
	}
```

**File:** connection_manager.go (L473-500)
```go
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
