### Title
Unbounded per-element append loops in certificate V2 details parsing allow CPU/allocation amplification from an unauthenticated handshake peer - (File: cert/cert_v2.go)

### Summary
`unmarshalDetails` in `cert/cert_v2.go` parses the ASN.1-encoded `networks`, `unsafeNetworks`, and `groups` fields of a certificate by looping `for !subString.Empty()` and repeatedly `append`-ing to Go slices with no bound on the number of elements, only a per-element length check (`MaxNetworkLength` for networks/unsafe networks, no analogous cap enforced on group count). This mirrors the `NFTXEligiblityManager.distribute` bug class: an unbounded iteration/accumulation over an attacker-supplied list, driven purely by attacker input, with no limit on the number of loop iterations independent of a single coarse byte-size gate.

### Finding Description
`cert.Recombine` → `unmarshalCertificateV2` → `unmarshalDetails` is reached during handshake processing in `handshake/machine.go`'s `validateCert`, which is called from `processPayload` for every handshake message that carries a certificate (`flags.expectsCert`) [1](#0-0) . This happens **before** the certificate is verified against the CA pool — `m.verifier(rc)` is only invoked after `cert.Recombine` has already fully parsed and internally validated the structure [2](#0-1) . Consequently, the ASN.1 unmarshalling code runs on certificate bytes supplied by any UDP peer initiating a handshake, without proof of a CA-signed certificate having been verified first.

`unmarshalCertificateV2` only bounds the *total* size of the certificate blob (`l > MaxCertificateSize`) [3](#0-2) , but the number of loop iterations inside `unmarshalDetails` for `networks`, `unsafeNetworks`, and `groups` is not separately capped — each loop simply continues `for !subString.Empty()` appending one element per iteration until the sub-string is drained [4](#0-3) . Because `MaxNetworkLength`/`MaxNameLength`-style checks only bound the byte length of *each* individual element, an attacker can fill the certificate with a very large number of minimal-size elements (e.g., many tiny groups or small-encoded network entries), maximizing element count per byte of certificate payload and forcing many `append` calls (each of which can trigger slice reallocation) purely as a function of attacker-chosen certificate content — with no separate cap on element counts. This is the direct analog of `distribute` iterating unboundedly over `_feeReceivers`: the loop's iteration count is entirely attacker-controlled and only indirectly bounded by an overall byte budget, not by an explicit maximum-element check.

### Impact Explanation
An attacker who sends a stage-1/stage-2 handshake packet with a crafted, self-signed (not necessarily CA-trusted) certificate can force the responder to spend disproportionate CPU/allocation time in `unmarshalDetails` for every handshake attempt, before any CA-pool trust check occurs. Because handshake processing happens per incoming UDP packet from unauthenticated sources (`HandshakeManager.HandleIncoming` → `beginHandshake`/`continueHandshake` → `Machine.ProcessPacket` → `processPayload` → `validateCert`), this allows a remote, unauthenticated peer to impose repeated parsing overhead on the node, contributing to a remote resource-exhaustion / denial-of-service vector against the handshake path, consistent with the "unbounded iteration causing excessive resource consumption, up to inability to process" impact class described in the source finding.

### Likelihood Explanation
Likelihood is moderate: the certificate size is still capped at `MaxCertificateSize`, which limits the absolute worst case, and per-element length checks exist for networks/unsafe networks. However, no explicit cap on the *number* of groups/networks entries is enforced independent of total byte budget, and this code path executes pre-authentication (before CA verification), meaning it is reachable by any peer capable of sending a UDP packet, without holding a CA-signed certificate.

### Recommendation
Add explicit, low maximum counts for the number of `networks`, `unsafeNetworks`, and `groups` entries parsed in `unmarshalDetails` (in `cert/cert_v2.go`), independent of the overall `MaxCertificateSize` byte budget, and reject certificates exceeding these counts before entering `validate()`/CA verification. Apply the same bound in the legacy `unmarshalCertificateV1` `Groups`/`Ips`/`Subnets` parsing path for consistency, since it is reached the same way from `cert.Recombine` during handshake processing.

### Proof of Concept
1. Craft a self-signed (or arbitrarily-signed, unverified-by-victim) certificate whose ASN.1 `TagDetailsGroups` field contains the maximum number of minimal 1-byte UTF8String group entries that fit under `MaxCertificateSize` (thousands of entries are feasible since each group entry can be a few bytes).
2. Encode this certificate into the `Cert` field of a stage-1 `NebulaHandshakeDetails` handshake payload and send it via `handshake/payload.go`'s `MarshalPayload` wire format to a target node's UDP listener, following the `HandshakeIXPSK0` subtype flow so it reaches `HandshakeManager.beginHandshake`.
3. Observe that `Machine.ProcessPacket` → `processPayload` → `validateCert` → `cert.Recombine` → `unmarshalCertificateV2` → `unmarshalDetails` executes the unbounded `for !subString.Empty()` group-parsing loop (`cert/cert_v2.go:702-710`) fully, performing thousands of `append` operations, before the certificate is ever checked against the CA pool.
4. Repeat with many distinct handshake attempts (varying `InitiatorIndex`/index to avoid dedup) from a single attacker host to amplify aggregate CPU cost on the victim with minimal attacker-side bandwidth, analogous to the referenced `distribute` unbounded-iteration DoS.

Note: I was not able to retrieve the exact numeric values of `MaxCertificateSize`/`MaxNetworkLength`/`MaxNameLength` due to search/indexing limits (grep matched but content wasn't returned in the available context), so the precise practical element-count ceiling implied by the byte-size cap could not be quantified from the index alone — a Devin session with full file access would be needed to confirm exact constants and construct a working byte-level PoC.

### Citations

**File:** handshake/machine.go (L332-379)
```go
	// Process certificate
	if flags.expectsCert {
		if err := m.validateCert(payload); err != nil {
			return err
		}
	}

	return nil
}

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

**File:** cert/cert_v2.go (L570-574)
```go
func unmarshalCertificateV2(b []byte, publicKey []byte, curve Curve) (*certificateV2, error) {
	l := len(b)
	if l == 0 || l > MaxCertificateSize {
		return nil, ErrBadFormat
	}
```

**File:** cert/cert_v2.go (L661-710)
```go
	var networks []netip.Prefix
	var val cryptobyte.String
	if found {
		for !subString.Empty() {
			if !subString.ReadASN1(&val, asn1.OCTET_STRING) || val.Empty() || len(val) > MaxNetworkLength {
				return detailsV2{}, ErrBadFormat
			}

			var n netip.Prefix
			if err := n.UnmarshalBinary(val); err != nil {
				return detailsV2{}, ErrBadFormat
			}
			networks = append(networks, n)
		}
	}

	// Read out any unsafe networks
	if !b.ReadOptionalASN1(&subString, &found, TagDetailsUnsafeNetworks) {
		return detailsV2{}, ErrBadFormat
	}

	var unsafeNetworks []netip.Prefix
	if found {
		for !subString.Empty() {
			if !subString.ReadASN1(&val, asn1.OCTET_STRING) || val.Empty() || len(val) > MaxNetworkLength {
				return detailsV2{}, ErrBadFormat
			}

			var n netip.Prefix
			if err := n.UnmarshalBinary(val); err != nil {
				return detailsV2{}, ErrBadFormat
			}
			unsafeNetworks = append(unsafeNetworks, n)
		}
	}

	// Read out any groups
	if !b.ReadOptionalASN1(&subString, &found, TagDetailsGroups) {
		return detailsV2{}, ErrBadFormat
	}

	var groups []string
	if found {
		for !subString.Empty() {
			if !subString.ReadASN1(&val, asn1.UTF8String) || val.Empty() {
				return detailsV2{}, ErrBadFormat
			}
			groups = append(groups, string(val))
		}
	}
```
