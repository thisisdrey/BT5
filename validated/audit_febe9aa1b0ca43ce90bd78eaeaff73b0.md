Based on my investigation, I found a strong analog to the reported bug class in the handshake certificate reconstruction path.

### Title
Unauthenticated `CertVersion` Field Selects Certificate Parser Before Signature Verification - (File: `handshake/machine.go`)

### Summary
The Linea report concerns a value (`l2MerkleTreesDepth`) that controls how proof data is *interpreted* without being cryptographically bound to that data, allowing the interpretation logic and the verified content to diverge. In Nebula's handshake path, the analogous unauthenticated control value is `payload.CertVersion`, an attacker-supplied field from the handshake payload that selects which certificate-unmarshalling routine (`unmarshalCertificateV1` vs `unmarshalCertificateV2`) is used to reconstruct and interpret the peer's certificate bytes, before any signature check occurs.

### Finding Description
During handshake processing, `Machine.validateCert` calls: [1](#0-0) 

`cert.Recombine` dispatches purely on the attacker-controlled `payload.CertVersion` field to select the parsing routine: [2](#0-1) 

The `payload.CertVersion` field is read directly off the wire with only a type/width check (must fit `uint32`), with no correlation to the actual bytes of `payload.Cert` or to any signature: [3](#0-2) 

This means the same raw `payload.Cert` byte string can be parsed by two structurally different, differently-serialized decoders (protobuf-based V1 vs ASN.1-based V2) purely because the attacker chose a different `CertVersion` tag, and this selection happens *before* `CheckSignature`/`VerifyCertificate` are invoked. Signature verification only occurs after parsing succeeds (inside `unmarshalCertificateV2`'s `c.validate()` step and later in `hm.certVerifier()`), so the version tag itself sits outside the cryptographically verified boundary at the moment it decides how bytes are decoded — structurally the same class of defect as the Linea issue, where a value dictating interpretation of proof-carrying data was not included in what is cryptographically bound.

The subsequent negotiation logic further amplifies this: after `Recombine` returns a certificate of whatever version it parsed, `myVersion` is switched to match the *peer-declared* version if a credential for it exists: [4](#0-3) 

### Impact Explanation
If the two format parsers (V1 protobuf vs V2 ASN.1) do not agree on a strict length/field discipline for all raw byte sequences, a single crafted `payload.Cert` blob could be a legitimately-signed V1 cert under one interpretation and, when reinterpreted as V2 (or vice versa) by an attacker simply flipping `CertVersion`, produce a differently-structured (but not necessarily re-verified against the *original* signature scope) certificate object. Because parsing occurs prior to verification, this also expands the pre-authentication parser attack surface reachable by any unauthenticated peer initiating a handshake (remote crash / DoS via malformed-but-differently-typed input is the most directly reachable risk; format-confusion-based signature bypass would require finding a byte sequence valid under both codecs' signed-region rules, which was not verified as currently exploitable in either direction based on the available context).

### Likelihood Explanation
This code path is reached by any unauthenticated peer that can send handshake packets — no valid CA-signed certificate is required to reach `validateCert`, since the version-selection and parsing happen before certificate trust is established. This matches the "no CA-signed certificate" reachability constraint.

### Recommendation
Bind the `CertVersion` field to material that is part of the certificate's signed/verified content (e.g., verify the parsed certificate's own `Version()` matches an expected value derived from something already authenticated, or require that the wire-level `CertVersion` be redundant/consistency-checked against a length/tag-derived version deduced from `payload.Cert` itself) rather than trusting an out-of-band attacker-supplied tag to pick the decoder before any signature is checked.

### Proof of Concept
Full exploitation requires constructing byte sequences that parse validly under both `unmarshalCertificateV1` (protobuf) and `unmarshalCertificateV2` (ASN.1) decoders with the version tag flipped between them — this specific confusion was not concretely demonstrated in the code reachable via search; the finding here documents the structural analog (unauthenticated version/format-selector field consumed before signature verification) rather than a proven forged-signature bypass. A minimal reproduction step: send a handshake message where `payload.CertVersion` is set to a value inconsistent with the actual encoding of `payload.Cert`, and observe whether `cert.Recombine` and the subsequent version-negotiation (`m.myVersion = rc.Version()`) accept/downgrade the effective certificate version without any signature having yet been checked against that specific reinterpretation.

### Citations

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

**File:** handshake/machine.go (L364-369)
```go
	// Version negotiation, if the peer sent a different version and we have it, switch
	if rc.Version() != m.myVersion {
		if m.getCred(rc.Version()) != nil {
			m.myVersion = rc.Version()
		}
	}
```

**File:** cert/cert.go (L128-159)
```go
func Recombine(v Version, rawCertBytes, publicKey []byte, curve Curve) (Certificate, error) {
	if publicKey == nil {
		return nil, ErrNoPeerStaticKey
	}

	if rawCertBytes == nil {
		return nil, ErrNoPayload
	}

	var c Certificate
	var err error

	switch v {
	// Implementations must ensure the result is a valid cert!
	case VersionPre1, Version1:
		c, err = unmarshalCertificateV1(rawCertBytes, publicKey)
	case Version2:
		c, err = unmarshalCertificateV2(rawCertBytes, publicKey, curve)
	default:
		return nil, ErrUnknownVersion
	}

	if err != nil {
		return nil, err
	}

	if c.Curve() != curve {
		return nil, fmt.Errorf("certificate curve %s does not match expected %s", c.Curve().String(), curve.String())
	}

	return c, nil
}
```

**File:** handshake/payload.go (L154-163)
```go
		case fieldCertVersion:
			if typ != protowire.VarintType {
				return errInvalidHandshakeDetails
			}
			v, n := protowire.ConsumeVarint(b)
			if n < 0 || v > math.MaxUint32 {
				return errInvalidHandshakeDetails
			}
			p.CertVersion = uint32(v)
			b = b[n:]
```
