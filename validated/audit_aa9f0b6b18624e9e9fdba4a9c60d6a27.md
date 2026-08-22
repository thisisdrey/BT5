### Title
Local host certificate is never validated against the loaded CA pool during PKI initialization or reload - (File: pki.go)

### Summary
The external report describes a `DInterest` contract that stores two duplicated pieces of related state — `moneyMarket` and `interestOracle` — and validates their consistency only in the setter (`setInterestOracle`) but not during initialization (`__DInterest_init`), letting the invariant "the oracle's money market must equal the contract's money market" silently break. The nebula analog is `PKI.reload` in `pki.go`, which loads two independently-derived, trust-critical pieces of state — the local host's own certificate(s) (`reloadCerts`) and the trusted CA pool (`reloadCAPool`) — but never checks the invariant that the local host certificate is actually signed by a CA present in that pool, either at initial load or on hot reload.

### Finding Description
`NewPKIFromConfig` calls `p.reload(c, true)`, which in turn calls `p.reloadCerts(c, initial)` followed by `p.reloadCAPool(c)`: [1](#0-0) 

`reloadCerts` builds a new `CertState` from `pki.cert`/`pki.key` and validates cert-to-cert consistency (matching public keys, curves, networks across v1/v2, and across reloads), but at no point checks the resulting certificate against any CA pool: [2](#0-1) 

`reloadCAPool` independently loads `pki.ca` into a `cert.CAPool` and stores it, with no reference back to the certificate state loaded by `reloadCerts`: [3](#0-2) 

`newCertState` — the constructor used by `reloadCerts` — verifies the private key matches the certificate's public key (`VerifyPrivateKey`), matches curves/networks between v1 and v2 certs, and builds the `myVpnNetworks*` tables, but it never receives or consults the CA pool at all: [4](#0-3) 

`loadCertificate` only checks expiry, that networks are present, and that the cert is not itself a CA — again, no linkage to the CA pool: [5](#0-4) 

The `cert.CAPool.VerifyCertificate`/`verify` machinery that would perform this check (signature validation, `GetCAForCert`, curve match, CA constraint checks) exists and is used elsewhere in the codebase — but only against *peer* certificates during the handshake, via `HandshakeManager.certVerifier()`: [6](#0-5) 

It is never invoked against the local node's own `v1Cert`/`v2Cert` produced by `newCertStateFromConfig`. This is structurally identical to the reported bug class: two related trust states (`interestOracle`↔`moneyMarket`, here `CertState`↔`CAPool`) are loaded from independent configuration inputs (`pki.cert`/`pki.key` vs `pki.ca`), and only one direction of a cross-check exists in the system (peer-cert verification during handshakes), while the "does my own identity match my own trust root" invariant is never enforced at initialization or reload time.

### Impact Explanation
If an operator misconfigures `pki.cert`/`pki.key` and `pki.ca` such that the local certificate is not actually signed by any CA in the configured pool (e.g., wrong CA file, stale cert from a rotated/retired CA, or a cert signed by a CA that was subsequently removed from `pki.ca`), the node will start successfully and begin sending handshake stage-0 packets built from an unverifiable identity via `buildStage0Packet`/`beginHandshake` in `handshake_manager.go`. Peers correctly reject the resulting handshake once they run `certVerifier()` against their CA pool, but the local node has no way to detect this condition itself: it will silently and indefinitely fail to establish tunnels with legitimate peers, producing a remote-state/availability failure indistinguishable from a network problem, with no diagnostic emitted at startup or reload. This is a state/consistency defect reachable purely through normal configuration loading, not through a malicious peer.

### Likelihood Explanation
This is a configuration-consistency bug rather than an externally attacker-triggered exploit; the "attacker" here is analogous to the report's exploit scenario (Alice misconfiguring `DInterest`) — an operator error during initial setup or CA rotation (e.g., swapping `pki.ca` while `pki.cert` still references the old CA, a common maintenance operation) can trigger it without any malicious actor. Given that `reloadCAPool` and `reloadCerts` are independent functions invoked back-to-back with no cross-validation, and CA rotation is an explicitly supported hot-reload scenario in this codebase, the likelihood of hitting this inconsistency during normal CA-rotation operations is non-trivial.

### Recommendation
- **Short term:** In `PKI.reload` (or at the end of `reloadCerts`/`reloadCAPool`), after both the new `CertState` and the new `cert.CAPool` are computed, verify that each present local certificate (`v1Cert`, `v2Cert`) successfully validates against the loaded CA pool via `caPool.VerifyCertificate(time.Now(), cert)` before calling `p.cs.Store(newState)` / `p.caPool.Store(caPool)`. Fail initialization (and log/reject the reload) if the local certificate is not verifiable under the current CA pool, mirroring how `setInterestOracle` enforces `interestOracle.moneyMarket() == moneyMarket`.
- **Long term:** Avoid loading/storing the certificate state and CA pool as two disjoint atomics without a single authoritative validation step; consider computing and validating both together in one function so the invariant "local identity is trusted by local CA pool" cannot be represented in an inconsistent state.

### Proof of Concept
1. Generate CA `A` and sign a host certificate `hostA.crt` with `A`'s key.
2. Configure `pki.cert`/`pki.key` to `hostA.crt`/its private key, and configure `pki.ca` to a *different* CA `B` (or an empty/rotated CA bundle that no longer contains `A`).
3. Start the node (or trigger `ReloadConfigString`/config reload with the new `pki.ca`). `NewPKIFromConfig`/`p.reload` succeeds: `reloadCerts` only checks internal cert consistency (`pki.go:97-194`, `pki.go:377-451`) and `reloadCAPool` only parses/loads CA `B` (`pki.go:196-205`, `pki.go:525-572`) — neither step cross-checks the other.
4. The node comes up "healthy" and attempts handshakes using `hostA.crt`. Peers verifying against CA `B` (or any CA pool not containing `A`) reject the handshake in `certVerifier()` (`handshake_manager.go:1161-1166`), causing persistent, silent connectivity failure with no startup-time error indicating the actual misconfiguration.

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

**File:** pki.go (L97-194)
```go
func (p *PKI) reloadCerts(c *config.C, initial bool) *util.ContextualError {
	var cipher string
	var currentState *CertState
	if initial {
		cipher = c.GetString("cipher", "aes")
		switch cipher {
		case "aes", "chachapoly":
			// Each post-handshake CipherState in noiseutil hardcodes its own
			// nonce endianness now, so there's nothing to set up here.
		default:
			return util.NewContextualError(
				"unknown cipher",
				m{"cipher": cipher},
				nil,
			)
		}
	} else {
		// Cipher cant be hot swapped so just leave it at what it was before
		currentState = p.cs.Load()
		cipher = currentState.cipher
	}

	newState, err := newCertStateFromConfig(c, cipher)
	if err != nil {
		return util.NewContextualError("Could not load client cert", nil, err)
	}

	if currentState != nil {
		if newState.v1Cert != nil {
			if currentState.v1Cert == nil {
				//adding certs is fine, actually. Networks-in-common confirmed in newCertState().
			} else {
				// did IP in cert change? if so, don't set
				if !slices.Equal(currentState.v1Cert.Networks(), newState.v1Cert.Networks()) {
					return util.NewContextualError(
						"Networks in new cert was different from old",
						m{"new_networks": newState.v1Cert.Networks(), "old_networks": currentState.v1Cert.Networks(), "cert_version": cert.Version1},
						nil,
					)
				}

				if currentState.v1Cert.Curve() != newState.v1Cert.Curve() {
					return util.NewContextualError(
						"Curve in new v1 cert was different from old",
						m{"new_curve": newState.v1Cert.Curve(), "old_curve": currentState.v1Cert.Curve(), "cert_version": cert.Version1},
						nil,
					)
				}
			}
		}

		if newState.v2Cert != nil {
			if currentState.v2Cert == nil {
				//adding certs is fine, actually
			} else {
				// did IP in cert change? if so, don't set
				if !slices.Equal(currentState.v2Cert.Networks(), newState.v2Cert.Networks()) {
					return util.NewContextualError(
						"Networks in new cert was different from old",
						m{"new_networks": newState.v2Cert.Networks(), "old_networks": currentState.v2Cert.Networks(), "cert_version": cert.Version2},
						nil,
					)
				}

				if currentState.v2Cert.Curve() != newState.v2Cert.Curve() {
					return util.NewContextualError(
						"Curve in new cert was different from old",
						m{"new_curve": newState.v2Cert.Curve(), "old_curve": currentState.v2Cert.Curve(), "cert_version": cert.Version2},
						nil,
					)
				}
			}

		} else if currentState.v2Cert != nil {
			//newState.v1Cert is non-nil bc empty certstates aren't permitted
			if newState.v1Cert == nil {
				return util.NewContextualError("v1 and v2 certs are nil, this should be impossible", nil, err)
			}
			//if we're going to v1-only, we need to make sure we didn't orphan any v2-cert vpnaddrs
			if !slices.Equal(currentState.v2Cert.Networks(), newState.v1Cert.Networks()) {
				return util.NewContextualError(
					"Removing a V2 cert is not permitted unless it has identical networks to the new V1 cert",
					m{"new_v1_networks": newState.v1Cert.Networks(), "old_v2_networks": currentState.v2Cert.Networks()},
					nil,
				)
			}
		}
	}

	p.cs.Store(newState)

	if initial {
		p.l.Debug("Client nebula certificate(s)", "cert", newState)
	} else {
		p.l.Info("Client certificate(s) refreshed from disk", "cert", newState)
	}
	return nil
}
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

**File:** pki.go (L377-451)
```go
func newCertState(dv cert.Version, v1, v2 cert.Certificate, pkcs11backed bool, privateKeyCurve cert.Curve, privateKey []byte, cipher string) (*CertState, error) {
	cs := CertState{
		privateKey:               privateKey,
		pkcs11Backed:             pkcs11backed,
		cipher:                   cipher,
		myVpnNetworksTable:       new(bart.Lite),
		myVpnAddrsTable:          new(bart.Lite),
		myVpnBroadcastAddrsTable: new(bart.Lite),
	}

	if v1 != nil && v2 != nil {
		if !slices.Equal(v1.PublicKey(), v2.PublicKey()) {
			return nil, util.NewContextualError("v1 and v2 public keys are not the same, ignoring", nil, nil)
		}

		if v1.Curve() != v2.Curve() {
			return nil, util.NewContextualError("v1 and v2 curve are not the same, ignoring", nil, nil)
		}

		if v1.Networks()[0] != v2.Networks()[0] {
			return nil, util.NewContextualError("v1 and v2 networks are not the same", nil, nil)
		}

		cs.initiatingVersion = dv
	}

	if v1 != nil {
		if pkcs11backed {
			//NOTE: We do not currently have a method to verify a public private key pair when the private key is in an hsm
		} else {
			if err := v1.VerifyPrivateKey(privateKeyCurve, privateKey); err != nil {
				return nil, fmt.Errorf("private key is not a pair with public key in nebula cert")
			}
		}

		v1hs, err := v1.MarshalForHandshakes()
		if err != nil {
			return nil, fmt.Errorf("error marshalling v1 certificate for handshake: %w", err)
		}
		ncs, err := newCipherSuite(v1.Curve(), pkcs11backed, cipher)
		if err != nil {
			return nil, err
		}
		cs.v1Cert = v1
		cs.v1Credential = handshake.NewCredential(v1, v1hs, privateKey, ncs)

		if cs.initiatingVersion == 0 {
			cs.initiatingVersion = cert.Version1
		}
	}

	if v2 != nil {
		if pkcs11backed {
			//NOTE: We do not currently have a method to verify a public private key pair when the private key is in an hsm
		} else {
			if err := v2.VerifyPrivateKey(privateKeyCurve, privateKey); err != nil {
				return nil, fmt.Errorf("private key is not a pair with public key in nebula cert")
			}
		}

		v2hs, err := v2.MarshalForHandshakes()
		if err != nil {
			return nil, fmt.Errorf("error marshalling v2 certificate for handshake: %w", err)
		}
		ncs, err := newCipherSuite(v2.Curve(), pkcs11backed, cipher)
		if err != nil {
			return nil, err
		}
		cs.v2Cert = v2
		cs.v2Credential = handshake.NewCredential(v2, v2hs, privateKey, ncs)

		if cs.initiatingVersion == 0 {
			cs.initiatingVersion = cert.Version2
		}
	}
```

**File:** pki.go (L504-523)
```go
func loadCertificate(b []byte) (cert.Certificate, []byte, error) {
	c, b, err := cert.UnmarshalCertificateFromPEM(b)
	if err != nil {
		return nil, b, fmt.Errorf("error while unmarshaling pki.cert: %w", err)
	}

	if c.Expired(time.Now()) {
		return nil, b, fmt.Errorf("nebula certificate for this host is expired")
	}

	if len(c.Networks()) == 0 {
		return nil, b, fmt.Errorf("no networks encoded in certificate")
	}

	if c.IsCA() {
		return nil, b, fmt.Errorf("host certificate is a CA certificate")
	}

	return c, b, nil
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
