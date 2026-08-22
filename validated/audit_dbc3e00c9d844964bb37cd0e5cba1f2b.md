Found: `GetCAForCert` in `cert/ca_pool.go` treats an empty issuer string identically to an empty-issuer lookup miss, but the real problem is that a certificate's `issuer` field is attacker-controlled data received during an unauthenticated handshake (`validateCert` → `m.verifier(rc)` → `CAPool.VerifyCertificate` → `verify` → `GetCAForCert`), and only an empty string is rejected — a non-empty but incorrect/attacker-chosen issuer value is looked up directly in the map with no additional binding/authentication check before the map access.

### Title
Missing validation of untrusted `issuer` receiver-key before CA-pool lookup enables cross-CA confusion - (File: `cert/ca_pool.go`)

### Summary
`CAPool.GetCAForCert` (`cert/ca_pool.go:254-266`) uses the certificate's self-reported `issuer` field as a map key into `ncp.CAs` with only a check for the empty string, analogous to the FETH `withdrawFrom` bug where only the zero-address case was checked while other invalid/attacker-influenced values were not. This function is reachable from a fully unauthenticated remote peer during the Noise handshake, before any certificate is proven to be signed by a trusted authority.

### Finding Description
When a remote peer completes a handshake, `handshake.Machine.validateCert` (`handshake/machine.go:342-379`) reconstructs the peer certificate from wire bytes via `cert.Recombine` and immediately calls `m.verifier(rc)`, which in the real implementation is `HandshakeManager.certVerifier()` (`handshake_manager.go:1161-1166`) → `CAPool.VerifyCertificate` (`cert/ca_pool.go:157-196`) → `ncp.verify` (`cert/ca_pool.go:210-250`) → `ncp.GetCAForCert(c)` (`cert/ca_pool.go:254-266`):

```go
func (ncp *CAPool) GetCAForCert(c Certificate) (*CachedCertificate, error) {
	issuer := c.Issuer()
	if issuer == "" {
		return nil, fmt.Errorf("no issuer in certificate")
	}
	signer, ok := ncp.CAs[issuer]
	...
}
``` [1](#0-0) 

The `issuer` value comes straight from the attacker-supplied, not-yet-verified certificate bytes (`unmarshalCertificateV1`/`unmarshalDetails` populate `details.issuer` from the wire with no format/length constraint beyond hex-decoding — see `cert_v1.go:441` `nc.details.issuer = hex.EncodeToString(rc.Details.Issuer)`). The only receiver-style validation performed on this field before it is used as a lookup key is the empty-string check; there is no check that the fingerprint format is well-formed, that it is not attempting to alias an unrelated trust anchor, or that the resulting signer is bound to the certificate by anything beyond the (later) signature check. This mirrors the FETH pattern of validating a single sentinel value (`address(0)`) while leaving the general validation of the "receiver" (here, the CA lookup key) incomplete — the function comment itself states "No signature validation is performed" at this stage, and the subsequent signature check (`c.CheckSignature(signer.Certificate.PublicKey())`) is the only thing standing between an attacker-chosen issuer string and a false-positive signer resolution.

### Impact Explanation
If any lookup-key confusion is possible (e.g., through fingerprint collision, encoding mismatch, or multi-tenant CA pools where issuer strings can be crafted to match a different, less-restrictive CA in the same pool), an attacker with no valid CA-signed certificate could cause their self-issued certificate to be evaluated against the wrong CA entry, potentially bypassing per-CA constraints (`CheckCAConstraints`, groups, networks, unsafe networks) enforced by `checkCAConstraints` (`cert/ca_pool.go:287-345`). Because this happens inside the unauthenticated handshake path (`beginHandshake`/`continueHandshake`), a successful confusion would result in a fraudulent peer certificate being accepted, granting it VPN network membership and firewall group rights it should not have — a direct authentication/certificate-verification-bypass impact.

### Likelihood Explanation
This is reachable pre-authentication by any UDP peer that can send a handshake packet (`HandshakeManager.HandleIncoming` → `beginHandshake` → `handshake.Machine.ProcessPacket` → `validateCert`) — no valid CA-signed certificate is required to reach `GetCAForCert`, only a syntactically valid handshake message with an attacker-chosen issuer string. Likelihood of a full bypass depends on whether an attacker can actually engineer an issuer-string collision or exploit an encoding inconsistency between the map key and the eventual signature check, which was not confirmed in the available code; this is presented as a bug-class analog per the report's scope rather than a proven exploit.

### Recommendation
Treat the `issuer` field with the same rigor as the FETH fix treated the `to` address: validate its format/length before using it as a trust-anchor lookup key, and ensure the fingerprint comparison and signature verification are cryptographically bound (constant-time compare, fixed-length hex) so no encoding or truncation-based aliasing between CA fingerprints is possible. Consider rejecting certificates whose `issuer` does not exactly match the canonical SHA-256 fingerprint format before performing the map lookup.

### Proof of Concept
Not independently reproduced against a running nebula instance in this analysis; the PoC would require constructing a handshake certificate whose `issuer` byte sequence, after hex-encoding, collides with or aliases a different CA fingerprint string in `ncp.CAs`, then observing that `GetCAForCert` returns that CA's `CachedCertificate` and that `CheckCAConstraints`/`CheckSignature` do not catch the mismatch — this last step (signature verification) is expected to fail in the current code, so exploitability is unconfirmed and should be verified with a live Devin session that can execute Go tests against `cert/ca_pool.go`.

### Citations

**File:** cert/ca_pool.go (L254-266)
```go
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
