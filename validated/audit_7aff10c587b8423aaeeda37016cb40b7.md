### Title
Tunnels remain open and continue passing traffic after the peer's certificate has expired or been revoked when `pki.disconnect_invalid` is left at its non-enforcing setting - (File: connection_manager.go)

### Summary
The Primitive Protocol report describes option tokens that remain freely transferable after expiry/pause even though minting and exercising are blocked, allowing stale/expired instruments to keep circulating. The analogous condition in nebula is that a tunnel's underlying identity document (the peer's certificate) can become expired or blocklisted, yet the tunnel is only torn down if `pki.disconnect_invalid` is enabled; otherwise traffic continues to flow over a tunnel established with a certificate that the local CA pool no longer considers valid.

### Finding Description
Certificate validity is only actively re-checked by the periodic `connectionManager` traffic-check loop, via `isInvalidCertificate`: [1](#0-0) 

This function calls `caPool.VerifyCachedCertificate(now, remoteCert)` and, if the certificate is expired (or the CA has become expired/removed) but not blocklisted, it only tears down the tunnel `else if cm.intf.disconnectInvalid.Load()`. If that flag is false, the function explicitly returns `false` and the tunnel stays alive: *"if we reach here, the cert is no longer valid, but we're configured to keep tunnels from now-invalid certs open"* (see the trailing `else` branch). The actual expiry check itself lives in `CAPool.verify`, which computes `c.Expired(now)`: [2](#0-1) 

Handshake-time validation (`certVerifier`) and `isInvalidCertificate` both call into the same `CAPool.VerifyCertificate`/`VerifyCachedCertificate` machinery, so an expired certificate is correctly rejected at handshake time: [3](#0-2) [4](#0-3) 

However, this only prevents *new* handshakes from completing with an expired cert — it does not stop an *already-established* tunnel from continuing to carry traffic once the peer certificate later expires, unless the operator has explicitly turned on `disconnect_invalid`. The periodic check itself only runs on the `connectionManager`'s traffic-check ticker (`checkInterval`, default from `timers.connection_alive_interval`), so even with the flag enabled there is a window during which traffic keeps flowing on a tunnel backed by an expired certificate: [5](#0-4) [6](#0-5) 

This mirrors the Option-contract analog precisely: the "mint"/"handshake" operation is properly gated on expiry, but the "transfer"/"continue using an existing tunnel" operation is not gated by default, and is only mitigated by an optional configuration flag whose absence silently preserves the vulnerable behavior. The CHANGELOG confirms this is a known, intentionally configurable trade-off rather than an oversight, though the default has changed over releases: [7](#0-6) 

### Impact Explanation
If `pki.disconnect_invalid` is not enabled (or a client predates the version where the default changed, or an operator has deliberately/accidentally left it disabled), a host whose certificate has expired — or whose signing CA has expired/been removed from the trust store — can continue to send and receive encrypted overlay traffic indefinitely over the previously established tunnel. This is a form of stale/expired-credential trust persistence: revocation (via CA rotation/expiry or blocklisting-adjacent expiry) does not actually sever an active session unless the specific opt-in flag is set, undermining the expectation that certificate expiry is an effective revocation mechanism for already-connected peers.

### Likelihood Explanation
This is reachable by any already-authenticated peer (no CA-signed cert forgery needed) whose certificate naturally expires while their tunnel remains active, or by an operator relying on cert expiry as a revocation mechanism without explicitly setting `pki.disconnect_invalid = true`. Given `disconnect_invalid` was historically `false` by default and only later changed, and remains a configurable per-deployment setting, misconfigured or older nodes are exposed. The blocklist path is enforced unconditionally, but plain expiry (the primary "credential lifetime" mechanism) is not.

### Recommendation
- Clarify in documentation that certificate expiry does not, by itself, terminate already-established tunnels unless `pki.disconnect_invalid` is enabled, and recommend enabling it by default for any deployment relying on cert lifetime for access control.
- Consider making expired-certificate teardown unconditional (like the blocklist case) rather than gated behind an opt-in flag, or at minimum shrink the enforcement window so expiry is checked more promptly and consistently across all active tunnels, not just at the next `checkInterval` tick.
- Ensure `tryRehandshake`/connection-manager logic re-validates certificates against updated CA pools promptly on every reload, not only on the traffic-check ticker.

### Proof of Concept
1. Establish a nebula tunnel between host A and host B using certificates signed by a CA, with `pki.disconnect_invalid` left at `false` (or omitted, if defaulting to false for the relevant nebula version).
2. Allow B's certificate `NotAfter` to elapse while the tunnel remains active (i.e., without a fresh handshake).
3. Continue sending traffic between A and B (e.g., via `sendTestPacket`/normal payload traffic) — `isInvalidCertificate` will detect `cert.Expired`, but because `disconnectInvalid` is `false`, `makeTrafficDecision` never returns `closeTunnel` for this reason, per [8](#0-7) .
4. Observe that traffic continues to flow across the tunnel indefinitely despite B holding an expired certificate, until some other unrelated teardown condition (inactivity, rehandshake failure, etc.) triggers.

### Citations

**File:** connection_manager.go (L139-164)
```go
func (cm *connectionManager) Start(ctx context.Context) {
	clockSource := time.NewTicker(cm.trafficTimer.t.tickDuration)
	defer clockSource.Stop()

	p := []byte("")
	nb := make([]byte, 12, 12)
	out := make([]byte, mtu)

	for {
		select {
		case <-ctx.Done():
			return

		case now := <-clockSource.C:
			cm.trafficTimer.Advance(now)
			for {
				localIndex, has := cm.trafficTimer.Purge()
				if !has {
					break
				}

				cm.doTrafficCheck(localIndex, p, nb, out, now)
			}
		}
	}
}
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

**File:** cert/ca_pool.go (L210-230)
```go
func (ncp *CAPool) verify(c Certificate, now time.Time, certFp string, signerFp string) (*CachedCertificate, error) {
	if ncp.IsBlocklisted(certFp) {
		return nil, ErrBlockListed
	}

	signer, err := ncp.GetCAForCert(c)
	if err != nil {
		return nil, err
	}

	if signer.Certificate.Curve() != c.Curve() {
		return nil, ErrCurveMismatch
	}

	if signer.Certificate.Expired(now) {
		return nil, ErrRootExpired
	}

	if c.Expired(now) {
		return nil, ErrExpired
	}
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

**File:** handshake/machine_test.go (L499-536)
```go
func TestMachineExpiredCert(t *testing.T) {
	ca, _, caKey, _ := ct.NewTestCaCert(
		cert.Version2, cert.Curve_CURVE25519,
		time.Now().Add(-24*time.Hour), time.Now().Add(24*time.Hour),
		nil, nil, nil,
	)
	caPool := ct.NewTestCAPool(ca)

	expCert, _, expKeyPEM, _ := ct.NewTestCert(
		cert.Version2, cert.Curve_CURVE25519, ca, caKey,
		"expired", time.Now().Add(-2*time.Hour), time.Now().Add(-1*time.Hour),
		[]netip.Prefix{netip.MustParsePrefix("10.0.0.1/24")}, nil, nil,
	)
	expKey, _, _, err := cert.UnmarshalPrivateKeyFromPEM(expKeyPEM)
	require.NoError(t, err)
	expHsBytes, err := expCert.MarshalForHandshakes()
	require.NoError(t, err)
	ncs := noise.NewCipherSuite(noise.DH25519, noise.CipherChaChaPoly, noise.HashSHA256)

	expiredCS := &testCertState{
		version: cert.Version2,
		creds: map[cert.Version]*Credential{
			cert.Version2: NewCredential(expCert, expHsBytes, expKey, ncs),
		},
	}

	respCS := newTestCertState(
		t, ca, caKey, "responder",
		[]netip.Prefix{netip.MustParsePrefix("10.0.0.2/24")},
	)

	_, respM, _, _, err := initiateHandshake(
		t, expiredCS, testVerifier(caPool),
		respCS, testVerifier(caPool),
	)
	require.ErrorContains(t, err, "verify cert")
	assert.True(t, respM.Failed())
}
```

**File:** CHANGELOG.md (L366-372)
```markdown
### Changed

- `pki.disconnect_invalid` will now default to true. This means that once a
  certificate expires, the tunnel will be disconnected. If you use SIGHUP to
  reload certificates without restarting Nebula, you should ensure all of your
  clients are on 1.7.0 or newer before you enable this feature. (#859)

```
