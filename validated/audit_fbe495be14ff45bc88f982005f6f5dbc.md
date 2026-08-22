### Title
CA blocklisting does not revoke certificates signed by the blocklisted CA - (File: `cert/ca_pool.go`)

### Summary
`CAPool.verify()` only checks the leaf certificate's own fingerprint against the blocklist, never the fingerprint of the CA that signed it. A CA that is added to `certBlocklist` remains fully present in `ncp.CAs` and continues to be used by `GetCAForCert` to validate any certificate that names it as issuer. This mirrors the Backd `RoleManager` finding: an operator performs a revocation action (`BlocklistFingerprint` on a compromised CA / `renounceGovernance`) and reasonably assumes trust has been removed, but the underlying data structure (`CAs` map / `_roleMembers`) still contains the "revoked" entity, so protection silently fails.

### Finding Description
`CAPool.AddCA` stores every added CA keyed by its fingerprint in `ncp.CAs`: [1](#0-0) 

`BlocklistFingerprint`/`IsBlocklisted` operate on a completely separate set (`certBlocklist`) and never touch `ncp.CAs`: [2](#0-1) 

The core verification routine `verify()` checks the blocklist only for the certificate being verified (`certFp`), then looks up the signer via `GetCAForCert`, and — critically — never checks whether the *signer's* fingerprint is blocklisted: [3](#0-2) 

`GetCAForCert` resolves the signer purely from `ncp.CAs[issuer]`, with no reference to `certBlocklist` at all: [4](#0-3) 

So if an operator responds to a CA key compromise by calling `BlocklistFingerprint(caFingerprint)` (the documented mechanism for revoking trust), the CA entry is still sitting in `ncp.CAs`, and `GetCAForCert` will happily return it as a valid signer for any certificate — including newly forged certificates signed with the stolen CA private key — as long as the leaf certificate's own fingerprint was never individually blocklisted. This is exactly the Backd bug pattern: the "revocation" primitive removes an *effective* member/authority (renounced governor / compromised CA) but the data structure used elsewhere to answer "is this still trusted" (`_roleMembers` length / `ncp.CAs` presence) is never updated, so the system keeps trusting it.

### Impact Explanation
This is a certificate/CA-pool verification bypass. Blocklisting a fingerprint is the only documented remediation path (`pki.blocklist` config, `BlocklistFingerprint`) for revoking a compromised or otherwise untrusted CA. Because the check is applied only to leaf certificate fingerprints and not to the signer chain, an attacker who has compromised (or ever obtained the private key of) a CA that an operator believes has been revoked can continue to mint certificates that pass `VerifyCertificate`/`VerifyCachedCertificate`, granting them full network identity/authentication as any host/group permitted by that CA's constraints. This is a full authentication-bypass path for the exact threat model the blocklist feature exists to mitigate.

### Likelihood Explanation
Likelihood is Medium: it requires an operator to have blocklisted a CA fingerprint (i.e., a CA compromise scenario already occurred) and an attacker in possession of that CA's private key to mint new certificates. Given that CA-fingerprint blocklisting is the primary remediation nebula documents for a compromised CA, this is a realistic and directly reachable failure of the remediation mechanism itself, not a contrived edge case.

### Recommendation
In `CAPool.verify()`, after resolving `signer` via `GetCAForCert`, add a check `if ncp.IsBlocklisted(signer.Fingerprint) { return nil, ErrBlockListed }` before proceeding with signature/constraint checks. Additionally consider removing/marking CA entries as untrusted in `ncp.CAs` directly when blocklisted (or exposing a `RemoveCA`/`GetTrustedCAs` accessor), analogous to the audit's recommendation to add a `getRoleMembers()` function that reflects only currently-active members, so callers cannot mistake presence-in-map for trust.

### Proof of Concept
1. Stand up a CA (`ca`) and use it to sign a leaf certificate.
2. Load the CA into a `CAPool` via `AddCA`.
3. Simulate CA compromise remediation: `caPool.BlocklistFingerprint(caFingerprint)`.
4. Call `caPool.VerifyCertificate(time.Now(), leafCert)` (or mint a brand-new certificate with the same CA key and verify it) — verification still succeeds because `verify()` never checks `IsBlocklisted(signer.Fingerprint)`, only `IsBlocklisted(certFp)` of the leaf itself. [5](#0-4)

### Citations

**File:** cert/ca_pool.go (L100-132)
```go
// AddCA verifies a Nebula CA certificate and adds it to the pool.
func (ncp *CAPool) AddCA(c Certificate) error {
	if !c.IsCA() {
		return fmt.Errorf("%s: %w", c.Name(), ErrNotCA)
	}

	if !c.CheckSignature(c.PublicKey()) {
		return fmt.Errorf("%s: %w", c.Name(), ErrNotSelfSigned)
	}

	sum, err := c.Fingerprint()
	if err != nil {
		return fmt.Errorf("could not calculate fingerprint for provided CA; error: %w; %s", err, c.Name())
	}

	cc := &CachedCertificate{
		Certificate:    c,
		Fingerprint:    sum,
		InvertedGroups: make(map[string]struct{}),
	}

	for _, g := range c.Groups() {
		cc.InvertedGroups[g] = struct{}{}
	}

	ncp.CAs[sum] = cc

	if c.Expired(time.Now()) {
		return fmt.Errorf("%s: %w", c.Name(), ErrExpired)
	}

	return nil
}
```

**File:** cert/ca_pool.go (L134-152)
```go
// BlocklistFingerprint adds a cert fingerprint to the blocklist
func (ncp *CAPool) BlocklistFingerprint(f string) {
	ncp.certBlocklist[f] = struct{}{}
}

// ResetCertBlocklist removes all previously blocklisted cert fingerprints
func (ncp *CAPool) ResetCertBlocklist() {
	ncp.certBlocklist = make(map[string]struct{})
}

// IsBlocklisted tests the provided fingerprint against the pools blocklist.
// Returns true if the fingerprint is blocked.
func (ncp *CAPool) IsBlocklisted(fingerprint string) bool {
	if _, ok := ncp.certBlocklist[fingerprint]; ok {
		return true
	}

	return false
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

**File:** cert/ca_pool.go (L252-266)
```go
// GetCAForCert attempts to return the signing certificate for the provided certificate.
// No signature validation is performed
func (ncp *CAPool) GetCAForCert(c Certificate) (*CachedCertificate, error) {
	issuer := c.Issuer()
	if issuer == "" {
		return nil, fmt.Errorf("no issuer in certificate")
	}

	signer, ok := ncp.CAs[issuer]
	if ok {
		return signer, nil
	}

	return nil, ErrCaNotFound
}
```
