### Title
Certificate-type confusion — peer's CA certificate is accepted as a host identity certificate because `CAPool.verify` never checks `IsCA()` - (File: cert/ca_pool.go)

### Summary
The `Certificate` interface documents that "It is invalid to use a CA certificate as a host certificate" [1](#0-0) , and this constraint *is* enforced when nebula loads its own local identity certificate at startup [2](#0-1) . However, the same check is missing from the shared verification path (`CAPool.verify`) that is used both to add CAs to the trust pool and — via `CertVerifier` — to validate a **peer's** certificate at the end of every handshake [3](#0-2) [4](#0-3) . This is structurally the same bug class as the reported `withdrawIncentives` issue: a function accepts an object (a certificate, analogous to a token address) without checking that its "type" field (`IsCA`, analogous to "is this the expected incentive token") matches what the calling context expects, letting a value meant for one privileged role (CA / governance token) be reused in a role reserved for a different, more restricted role (host identity / incentive token).

### Finding Description
`CAPool.verify` performs issuer lookup, curve match, expiry checks, signature verification, and `CheckCAConstraints`, but at no point checks `c.IsCA()`: [5](#0-4) 

This function backs both `VerifyCertificate` and `VerifyCachedCertificate` [6](#0-5) , which is exactly what `HandshakeManager.certVerifier` hands to the handshake `Machine` as the `CertVerifier`: [3](#0-2) 

During the handshake, `Machine.validateCert` reconstructs the peer certificate from the wire (`cert.Recombine`) and passes it straight to this verifier; it only checks that the public key matches the Noise static key and that the curve/version line up — never that the certificate is a non-CA (host) certificate: [4](#0-3) 

By contrast, when nebula loads its **own** `pki.cert` at startup it explicitly rejects a CA certificate being used as the local host identity: [7](#0-6) 

That equivalent guard is absent from the peer-verification path. Any certificate that is `isCA=true` but is itself validly signed by a trusted CA in the pool (an intermediate/sub-CA certificate) — carrying whatever `Networks`/`Groups`/lifetime the signing CA constrained it to — will pass `VerifyCertificate`/`VerifyCachedCertificate` and be accepted by the handshake machine as if it were an ordinary host certificate, exactly as the incentives contract accepted the governance token address as if it were an incentive token because it never checked the token's expected "role".

### Impact Explanation
A holder of any intermediate CA credential in the trust pool (a role intended only for issuing further certificates, never for terminating tunnels) can authenticate as a full mesh peer using that CA cert/key pair directly in the Noise handshake. This is a certificate-verification-bypass / role-confusion bug: it violates the documented security invariant "it is invalid to use a CA certificate as a host certificate" [1](#0-0) , and it means the CA-cert holder is not subjected to whatever additional operational assumptions nebula makes about host certificates (e.g., that `IsCA()==false` peers cannot themselves be trusted signers, that CA fingerprints are only ever used for the trust pool, not as live tunnel identities). It also means a compromised or misused CA key becomes directly usable to join the network as an authenticated node rather than only to sign new certificates, expanding the blast radius of a CA key compromise.

### Likelihood Explanation
Exploitation requires possession of a valid CA certificate and its private key that is present in the target's trusted CA pool (e.g., an intermediate CA used only for delegated signing). This is a lower bar than compromising the root, and organizations using intermediate/sub-CAs for signing delegation are directly exposed. Because the check is simply absent (not merely misordered), the condition is deterministic and trivially reproducible once a CA key is available — no race condition or timing dependency is involved.

### Recommendation
Add an explicit check in `CAPool.verify` (or in `CertVerifier`/`Machine.validateCert`) that rejects the peer certificate if `c.IsCA()` is true, mirroring the guard already present in `loadCertificate` for local host certificates:
```go
if c.IsCA() {
    return nil, ErrCACert // new sentinel error, e.g. "certificate is a CA certificate"
}
```
This should be enforced before (or as part of) `ncp.verify` so both `VerifyCertificate` and `VerifyCachedCertificate` — and therefore the handshake `CertVerifier` — refuse to authenticate a peer using a CA certificate.

### Proof of Concept
1. Stand up a nebula CA and issue an intermediate/sub-CA certificate `subCA.crt`/`subCA.key` with `-ca` flag as normal, ensuring it lands in the trusted `pki.ca` pool file of the target network.
2. Configure a rogue node with `pki.cert = subCA.crt` and `pki.key = subCA.key` (bypassing the normal `nebula-cert sign` host-issuance flow — this is possible because nothing in the handshake path calls `loadCertificate`'s IsCA guard on the *peer's* wire-transmitted certificate, only on the locally configured cert of each side).
3. Initiate a handshake against a legitimate nebula node in the mesh. `Machine.validateCert` reconstructs the sub-CA certificate from the wire payload and forwards it to `HandshakeManager.certVerifier`, which calls `CAPool.VerifyCertificate` [8](#0-7) . Because the sub-CA cert is validly signed by the pool's trusted root, `verify()` finds a signer, matches curve, checks expiry, verifies the signature, and passes `CheckCAConstraints` — with no `IsCA()` check anywhere in that call chain [5](#0-4) .
4. The handshake completes successfully with the rogue node authenticated as a normal tunnel peer using a certificate that was only ever supposed to be used for signing other certificates, not for establishing tunnels — exactly analogous to using the governance token address in `withdrawIncentives` to bypass the token-type check meant to gate the incentive-only withdrawal path.

### Citations

**File:** cert/cert.go (L44-46)
```go
	// IsCA signifies if this is a certificate authority (true) or a host certificate (false).
	// It is invalid to use a CA certificate as a host certificate.
	IsCA() bool
```

**File:** pki.go (L504-522)
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

**File:** handshake/machine.go (L342-379)
```go
func (m *Machine) validateCert(payload Payload) error {
	cred := m.getCred(m.myVersion)
	if cred == nil {
		m.failed = true
		return fmt.Errorf("%w: %v", ErrNoCredential, m.myVersion)
	}
	rc, err := cert.Recombine(
		cert.Version(payload.CertVersion),
		payload.Cert,
		m.hs.PeerStatic(),
		cred.Cert.Curve(),
	)
	if err != nil {
		m.failed = true
		return fmt.Errorf("recombine cert: %w", err)
	}

	if !bytes.Equal(rc.PublicKey(), m.hs.PeerStatic()) {
		m.failed = true
		return ErrPublicKeyMismatch
	}

	// Version negotiation, if the peer sent a different version and we have it, switch
	if rc.Version() != m.myVersion {
		if m.getCred(rc.Version()) != nil {
			m.myVersion = rc.Version()
		}
	}

	verified, err := m.verifier(rc)
	if err != nil {
		m.failed = true
		return fmt.Errorf("verify cert: %w", err)
	}

	m.result.RemoteCert = verified
	m.remoteCertSet = true
	return nil
```

**File:** cert/ca_pool.go (L154-208)
```go
// VerifyCertificate verifies the certificate is valid and is signed by a trusted CA in the pool.
// If the certificate is valid then the returned CachedCertificate can be used in subsequent verification attempts
// to increase performance.
func (ncp *CAPool) VerifyCertificate(now time.Time, c Certificate) (*CachedCertificate, error) {
	if c == nil {
		return nil, fmt.Errorf("no certificate")
	}
	fp, err := c.Fingerprint()
	if err != nil {
		return nil, fmt.Errorf("could not calculate fingerprint to verify: %w", err)
	}

	signer, err := ncp.verify(c, now, fp, "")
	if err != nil {
		return nil, err
	}

	// Pre nebula v1.10.3 could generate signatures in either high or low s form and validation
	// of signatures allowed for either. Nebula v1.10.3 and beyond clamps signature generation to low-s form
	// but validation still allows for either. Since a change in the signature bytes affects the fingerprint, we
	// need to test both forms until such a time comes that we enforce low-s form on signature validation.
	fp2, err := CalculateAlternateFingerprint(c)
	if err != nil {
		return nil, fmt.Errorf("could not calculate alternate fingerprint to verify: %w", err)
	}
	if fp2 != "" && ncp.IsBlocklisted(fp2) {
		return nil, ErrBlockListed
	}

	cc := CachedCertificate{
		Certificate:       c,
		InvertedGroups:    make(map[string]struct{}),
		Fingerprint:       fp,
		fingerprint2:      fp2,
		signerFingerprint: signer.Fingerprint,
	}

	for _, g := range c.Groups() {
		cc.InvertedGroups[g] = struct{}{}
	}

	return &cc, nil
}

// VerifyCachedCertificate is the same as VerifyCertificate other than it operates on a pre-verified structure and
// is a cheaper operation to perform as a result.
func (ncp *CAPool) VerifyCachedCertificate(now time.Time, c *CachedCertificate) error {
	// Check any available alternate fingerprint forms for this certificate, re P256 high-s/low-s
	if c.fingerprint2 != "" && ncp.IsBlocklisted(c.fingerprint2) {
		return ErrBlockListed
	}

	_, err := ncp.verify(c.Certificate, now, c.Fingerprint, c.signerFingerprint)
	return err
}
```

**File:** cert/ca_pool.go (L210-250)
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

	// If we are checking a cached certificate then we can bail early here
	// Either the root is no longer trusted or everything is fine
	if len(signerFp) > 0 {
		if signerFp != signer.Fingerprint {
			return nil, ErrFingerprintMismatch
		}
		return signer, nil
	}
	if !c.CheckSignature(signer.Certificate.PublicKey()) {
		return nil, ErrSignatureMismatch
	}

	err = CheckCAConstraints(signer.Certificate, c)
	if err != nil {
		return nil, err
	}

	return signer, nil
}
```
