### Title
Unbounded certificate list parsing in `unmarshalDetails` allows pre-authentication CPU/memory amplification during handshake - (File: `cert/cert_v2.go`)

### Summary
`unmarshalDetails` in `cert/cert_v2.go` parses the `networks`, `unsafeNetworks`, and `groups` fields of a Nebula v2 certificate using `for !subString.Empty() { ... }` loops that keep appending to unbounded Go slices, with no cap on the number of entries. This routine is reached via `cert.Recombine` from `handshake.Machine.validateCert`, which runs on every inbound handshake packet **before** the peer's certificate is verified against the CA pool. An attacker with no CA-signed certificate can therefore trigger this unbounded parsing/allocation work simply by sending a crafted handshake message.

### Finding Description
`unmarshalCertificateV2` bounds only the overall certificate size (`MaxCertificateSize`, 64KB) and per-entry length (`MaxNetworkLength` for each network/unsafe-network octet string), but places no bound on the *count* of entries read in each list: [1](#0-0) [2](#0-1) [3](#0-2) 

Because each list entry can be minimal in size (a few bytes each for networks/unsafe-networks, or a very short UTF8String for groups), an attacker can pack a very large number of list entries into a single certificate blob within the 64KB `MaxCertificateSize` budget, causing many slice `append` calls and memory allocations per parse — this mirrors the unbounded-loop pattern in the original report (`_transferERC20sOut`/`_transferERC721sOut`/`_transferFloorsOut` looping over an attacker-influenced, unbounded list).

Critically, this parsing happens on the handshake path *before* the certificate is checked against any CA pool: [4](#0-3) 

`validateCert` calls `cert.Recombine`, which dispatches to `unmarshalCertificateV2` → `unmarshalDetails`, and only calls `m.verifier(rc)` (the CA-pool signature/trust check) afterward: [5](#0-4) [6](#0-5) 

So the expensive unbounded parsing occurs for *any* peer that can complete only the network-level handshake framing, regardless of whether their certificate is trusted or even self-consistent — no CA-signed certificate is required to reach this code path.

### Impact Explanation
Each handshake attempt from an unauthenticated remote peer can force the responder to allocate and process an attacker-chosen number of `netip.Prefix` and `string` entries before any trust decision is made. Because handshakes have no pre-existing rate limit tied to a valid identity (the identity isn't established until parsing+verification complete), this allows a remote, unauthenticated party to repeatedly drive CPU and memory allocation work on the target with crafted certificate payloads — a remote resource-exhaustion / DoS vector, consistent with the "unbounded loop" bug class in the source report (loss of service/availability rather than direct fund loss, but same root cause: no upper bound on loop iterations over attacker-supplied data).

### Likelihood Explanation
High likelihood of reachability: handshake processing is inherently exposed to any UDP peer, and `validateCert` is invoked automatically as part of standard handshake message processing, prior to CA verification. No valid or CA-signed certificate is needed — only a syntactically well-formed but semantically arbitrary ASN.1 certificate blob within the existing size ceiling.

### Recommendation
Add an explicit maximum count (not just per-item length) for `networks`, `unsafeNetworks`, and `groups` in `unmarshalDetails`, rejecting certificates that exceed a small, fixed number of entries (e.g., matching whatever reasonable operational maximum nebula deploys support), independent of the overall `MaxCertificateSize` bound. This closes the gap where many small entries can still produce excessive loop iterations/allocations within the byte-size budget.

### Proof of Concept
1. Craft a v2 certificate `Details` ASN.1 structure whose `groups` (or `networks`/`unsafeNetworks`) SEQUENCE contains tens of thousands of minimal-length entries, staying under `MaxCertificateSize` (65536 bytes).
2. Send this certificate as the `Cert` field of a handshake payload to a nebula node acting as a lighthouse or listener, without possessing any CA-signed certificate trusted by that node.
3. Observe that `handshake.Machine.validateCert` → `cert.Recombine` → `unmarshalCertificateV2` → `unmarshalDetails` fully parses and allocates all list entries via the unbounded `for !subString.Empty() { ... append(...) }` loops before `m.verifier(rc)` is ever invoked and the handshake is rejected — i.e., the costly work happens pre-trust-decision and can be repeated at will by an unauthenticated attacker. [7](#0-6)

### Citations

**File:** cert/cert_v2.go (L570-574)
```go
func unmarshalCertificateV2(b []byte, publicKey []byte, curve Curve) (*certificateV2, error) {
	l := len(b)
	if l == 0 || l > MaxCertificateSize {
		return nil, ErrBadFormat
	}
```

**File:** cert/cert_v2.go (L661-675)
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
```

**File:** cert/cert_v2.go (L682-695)
```go
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
```

**File:** cert/cert_v2.go (L702-710)
```go
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

**File:** handshake/machine.go (L342-357)
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
```

**File:** handshake/machine.go (L359-375)
```go
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
```

**File:** cert/cert.go (L140-148)
```go
	switch v {
	// Implementations must ensure the result is a valid cert!
	case VersionPre1, Version1:
		c, err = unmarshalCertificateV1(rawCertBytes, publicKey)
	case Version2:
		c, err = unmarshalCertificateV2(rawCertBytes, publicKey, curve)
	default:
		return nil, ErrUnknownVersion
	}
```
