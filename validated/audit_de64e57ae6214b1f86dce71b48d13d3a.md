## Title
Missing/zero `CertVersion` in handshake payload causes peer certificate to be recombined under the wrong wire format - (File: handshake/machine.go)

### Summary
The external report's root cause is a struct-initialization gap: an omitted field falls back to its zero value, and that zero value is fed into a downstream security check that assumes the field was always explicitly set, causing behavior the caller did not intend. The `deadline` field defaults to `0`, which is silently treated by Uniswap as "already expired." The closest reachable analog in this codebase is `handshake.Payload.CertVersion`: it is an optional wire field, defaults to `0` on the unmarshalled side, and that raw attacker-controlled `0` is passed directly into `cert.Recombine` to select which certificate parser/format is used to reconstruct the peer's identity during the handshake, before any CA verification occurs.

### Finding Description
`Payload.CertVersion` is transmitted as an optional protobuf-style varint field. On the wire encoder side, `MarshalPayload` omits the field entirely when it is `0`: [1](#0-0) . On the decoder side, if the field is absent from the wire bytes, `Payload.CertVersion` simply keeps its Go zero value (`0`), because `unmarshalPayloadDetails` only assigns the field when the corresponding tag is actually present in the message: [2](#0-1) .

`Machine.validateCert` then takes that value from the wire, without any explicit "was this field present" check, and passes it straight into `cert.Recombine` to select the certificate unmarshalling codepath: [3](#0-2) 

`cert.Recombine` switches on this version value to choose between the legacy protobuf-based V1/Pre1 parser and the ASN.1-based V2 parser: [4](#0-3) 

Because `payload.CertVersion` is fully attacker-controlled wire input (it comes from the unauthenticated peer's handshake message, before the peer's certificate has been validated against any CA), an attacker with no CA-signed certificate can freely set this field to `0` (or simply omit it) to force `Recombine` down the `VersionPre1`/`Version1` code path regardless of what certificate format the attacker's cert bytes actually are, since nothing in `validateCert` cross-checks that the omitted/zero `CertVersion` was legitimate before the parse attempt. This is structurally identical to the reported bug class: a field that is optional-by-omission on the wire silently resolves to its zero value, and that zero value is trusted as meaningful input to a security-relevant decision (which cryptographic/verification code path processes untrusted bytes) rather than being treated as "not specified."

### Impact Explanation
This sits squarely in the "certificate parsing / handshake authentication" reachable surface: it operates on the responder's/initiator's processing of an unauthenticated peer's handshake payload, before `CertVerifier`/CA-pool verification has run. If the V1/Pre1 and V2 certificate parsers differ in strictness or field validation (e.g., V1's looser network/curve handling versus V2's ASN.1 constraints — as seen in the differing `validate()` implementations for `certificateV1` vs `certificateV2`), an attacker can coerce the responder into parsing untrusted certificate bytes with a parser of the attacker's choosing rather than the one that matches the certificate's actual encoded structure, by manipulating a field that silently defaults to zero. At minimum this is a robustness/DoS-relevant parsing-confusion primitive reachable pre-authentication; at most (depending on differences between the V1 and V2 validate() paths) it could be leveraged to smuggle a malformed/borderline certificate through the "wrong" validator's leniency before CA-signature verification even executes.

### Likelihood Explanation
High reachability: this code executes during the payload/cert-processing phase of `Machine.ProcessPacket` on unauthenticated peers (`processPayload` → `validateCert`), i.e., exactly where an attacker with no CA-signed certificate can send a crafted handshake message. No prior trust relationship is required, matching the report's "reachable by an attacker with no CA-signed certificate" scope, and the zero-default is trivially attacker-controlled (just don't set the field, or set it to 0), mirroring how easy it is to leave `deadline: 0` unset in the original report.

### Recommendation
Do not let a wire-omitted/zero `CertVersion` implicitly select a parser. Options:
- Require `CertVersion` to be explicitly meaningful for the negotiated handshake, and reject payloads where `CertVersion` is `0` but a `Cert` is present and the locally expected version is `Version2` (or vice versa), instead of silently trying `cert.Version(0)`.
- Add a presence flag (similar to how `InitiatorIndex`/`ResponderIndex`/`Time` are checked via `hasPayloadData`) so "field absent" is distinguishable from "field is legitimately 0," and fail closed (`ErrUnexpectedContent`/similar) when cert bytes are present without an explicit, expected version.
- Cross-validate that the recombined certificate's parsed structure is self-consistent with the version actually used before trusting `rc.Version()` for subsequent negotiation logic in `validateCert`.

### Proof of Concept
1. An unauthenticated peer initiates or responds to a handshake and crafts a `NebulaHandshakeDetails` payload containing a `Cert` field but omitting the `CertVersion` field (tag 8) entirely, or explicitly encoding it as `0`.
2. On decode, `unmarshalPayloadDetails` never touches `p.CertVersion` for the missing tag case, leaving it at the Go zero value `0`: `handshake/payload.go:154-163`.
3. `processPayload` sees `hasCertData := len(payload.Cert) > 0` is true and proceeds to `validateCert` unconditionally: `handshake/machine.go:307-337`.
4. `validateCert` calls `cert.Recombine(cert.Version(0), payload.Cert, ...)`, which routes to the `VersionPre1, Version1` case in the switch regardless of the responder's/initiator's actually configured/expected version: `handshake/machine.go:348-353`, `cert/cert.go:140-148`.
5. The certificate bytes are now parsed by the V1 unmarshaller/validator instead of whichever parser actually matches the peer's real certificate encoding, before any CA/signature check has occurred — demonstrating that an attacker fully controls which certificate-parsing code path processes their unauthenticated input via a field that silently defaults to zero.

Note: I was unable to fully trace whether the V1 vs V2 `validate()` leniency differences (e.g., `certificateV1.validate()` in `cert/cert_v1.go:332-382` vs `certificateV2.validate()` in `cert/cert_v2.go:391-460`) can be escalated into a concrete verification bypass beyond parser confusion — that would require deeper analysis of how each parser's constraints interact with the subsequent `CertVerifier`/CA-pool checks, which is a good next step for a background agent with full-repo/test-execution access.

### Citations

**File:** handshake/payload.go (L56-59)
```go
	if p.CertVersion != 0 {
		details = protowire.AppendTag(details, fieldCertVersion, protowire.VarintType)
		details = protowire.AppendVarint(details, uint64(p.CertVersion))
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

**File:** cert/cert.go (L128-148)
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
```
