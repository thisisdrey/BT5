### Title
Missing peer public key / signature length validation in certificate unmarshaling allows malformed key material into the Noise handshake - ([File: cert/cert_v1.go])

### Summary
The external report flags `AbyssEth2Depositor.deposit` for accepting BLS12-381 signature/pubkey/withdrawal-credential byte blobs with no length checks, which can silently corrupt cryptographic state. The reachable analog in this codebase is `certificateV1.validate()`, which only checks that the certificate's embedded public key is non-empty, never that it matches the expected byte length for its curve, before that key is fed into the Noise `StaticKeypair` used for the ECDH handshake.

### Finding Description
`certificateV1.validate()` performs only an emptiness check on the subject public key: [1](#0-0) 
There is no check that `len(c.details.publicKey)` equals 32 bytes (X25519) or 65 bytes (uncompressed P256), unlike the strict length checks enforced elsewhere for PEM-encoded keys, e.g. `UnmarshalPublicKeyFromPEM`/`UnmarshalSigningPublicKeyFromPEM`: [2](#0-1) 
The signature field is similarly only checked for non-emptiness in `setSignature`, not for an expected length: [3](#0-2) 
This under-validated certificate is exactly what `Recombine`/`unmarshalCertificateV1` produces from attacker-controlled handshake bytes (public key + raw cert bytes received from an unauthenticated peer during handshake), with no post-unmarshal length assertion beyond `validate()`: [4](#0-3) [5](#0-4) 
The certificate's `PublicKey()` is then passed directly into the Noise handshake state as the peer's static DH key without any additional length validation in the handshake credential path: [6](#0-5) 

### Impact Explanation
For `Curve_CURVE25519`, an undersized/oversized `details.publicKey` reaching `noise.DHKey{Public: ...}` risks either a panic (index-out-of-range in the DH implementation) or, if the underlying X25519 library tolerates non-32-byte input via truncation/padding, a deterministic but non-standard shared secret — a class of bug historically associated with weak/degenerate handshake keys. This is reachable pre-authentication from any peer presenting a certificate during the Noise handshake (`Machine.ProcessPacket`/`Initiate`), i.e. before any CA-signed trust decision is fully applied to the raw key bytes themselves. Impact is bounded to the CURVE25519 path in `certificateV1`; `certificateV2`'s ASN.1 unmarshaling additionally rejects empty public keys (`len(rawPublicKey) == 0`) but likewise does not assert the exact expected byte length for the negotiated curve before use.

### Likelihood Explanation
Likelihood is moderate: `CheckSignature` does independently guard the *CA's* verification key length (`len(key) != ed25519.PublicKeySize`) before calling `ed25519.Verify`, which prevents a signature-check panic for the CA-side key. However, that guard is on the CA's key used to verify the certificate signature, not on the certificate's own embedded subject public key, which is the value forwarded into the Noise handshake as the peer's DH public key. Whether Go's `curve25519.X25519`/`noise.DH25519` implementation panics, silently truncates, or errors on a non-32-byte key would need to be confirmed by tracing the noise library's `DH25519` implementation, which was not available in the indexed context.

### Recommendation
In `certificateV1.validate()` (and analogously in `certificateV2.validate()`/`unmarshalDetails`), assert `len(c.details.publicKey)` matches the exact expected length for `c.details.curve` (32 bytes for `Curve_CURVE25519`, 65 bytes uncompressed for `Curve_P256`), and assert the signature length matches the expected size for the curve (64 bytes for Ed25519; DER-encoded bound for P256) before the certificate is considered valid or its key material is handed to the Noise handshake state.

### Proof of Concept
Not independently reproducible from the indexed context alone — this requires constructing a `RawNebulaCertificate` protobuf with a `PublicKey` field of a non-32-byte length for `Curve_CURVE25519`, feeding it through `unmarshalCertificateV1`/`Recombine` during a handshake, and observing whether `noise.DHKey{Public: shortKey}` panics or produces a degenerate shared secret in `handshake.Credential.buildHandshakeState`. This would need to be verified with actual test execution against the `flynn/noise` DH25519 implementation, which is outside what the code index could confirm.

### Citations

**File:** cert/cert_v1.go (L332-337)
```go
func (c *certificateV1) validate() error {
	// Empty names are allowed

	if len(c.details.publicKey) == 0 {
		return ErrInvalidPublicKey
	}
```

**File:** cert/cert_v1.go (L392-398)
```go
func (c *certificateV1) setSignature(b []byte) error {
	if len(b) == 0 {
		return ErrEmptySignature
	}
	c.signature = b
	return nil
}
```

**File:** cert/cert_v1.go (L400-454)
```go
// unmarshalCertificateV1 will unmarshal a protobuf byte representation of a nebula cert
// if the publicKey is provided here then it is not required to be present in `b`
func unmarshalCertificateV1(b []byte, publicKey []byte) (*certificateV1, error) {
	if len(b) == 0 {
		return nil, fmt.Errorf("nil byte array")
	}
	var rc RawNebulaCertificate
	err := proto.Unmarshal(b, &rc)
	if err != nil {
		return nil, err
	}

	if rc.Details == nil {
		return nil, fmt.Errorf("encoded Details was nil")
	}

	if len(rc.Details.Ips)%2 != 0 {
		return nil, fmt.Errorf("encoded IPs should be in pairs, an odd number was found")
	}

	if len(rc.Details.Subnets)%2 != 0 {
		return nil, fmt.Errorf("encoded Subnets should be in pairs, an odd number was found")
	}

	nc := certificateV1{
		details: detailsV1{
			name:           rc.Details.Name,
			groups:         make([]string, len(rc.Details.Groups)),
			networks:       make([]netip.Prefix, len(rc.Details.Ips)/2),
			unsafeNetworks: make([]netip.Prefix, len(rc.Details.Subnets)/2),
			notBefore:      time.Unix(rc.Details.NotBefore, 0),
			notAfter:       time.Unix(rc.Details.NotAfter, 0),
			publicKey:      nil,
			isCA:           rc.Details.IsCA,
			curve:          rc.Details.Curve,
		},
		signature: make([]byte, len(rc.Signature)),
	}

	copy(nc.signature, rc.Signature)
	copy(nc.details.groups, rc.Details.Groups)
	nc.details.issuer = hex.EncodeToString(rc.Details.Issuer)

	// If a public key is passed in as an argument, the certificate pubkey must be empty
	// and the passed-in pubkey copied into the cert.
	if len(publicKey) > 0 {
		if len(rc.Details.PublicKey) != 0 {
			return nil, ErrCertPubkeyPresent
		}
		nc.details.publicKey = make([]byte, len(publicKey))
		copy(nc.details.publicKey, publicKey)
	} else {
		nc.details.publicKey = make([]byte, len(rc.Details.PublicKey))
		copy(nc.details.publicKey, rc.Details.PublicKey)
	}
```

**File:** cert/pem.go (L159-176)
```go
	var expectedLen int
	var curve Curve
	switch k.Type {
	case X25519PublicKeyBanner:
		expectedLen = 32
		curve = Curve_CURVE25519
	case P256PublicKeyBanner:
		// Uncompressed
		expectedLen = 65
		curve = Curve_P256
	default:
		return nil, r, 0, fmt.Errorf("bytes did not contain a proper public key banner")
	}
	if len(k.Bytes) != expectedLen {
		return nil, r, 0, fmt.Errorf("key was not %d bytes, is invalid %s public key", expectedLen, curve)
	}
	return k.Bytes, r, curve, nil
}
```

**File:** cert/cert.go (L124-152)
```go
// Recombine will attempt to unmarshal a certificate received in a handshake.
// Handshakes save space by placing the peers public key in a different part of the packet, we have to
// reassemble the actual certificate structure with that in mind.
// Implementations MUST assert the public key is not in the raw certificate bytes if the passed in public key is not empty.
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
```

**File:** handshake/credential.go (L37-48)
```go
// buildHandshakeState creates a noise.HandshakeState from this credential.
func (hc *Credential) buildHandshakeState(initiator bool, pattern noise.HandshakePattern) (*noise.HandshakeState, error) {
	return noise.NewHandshakeState(noise.Config{
		CipherSuite:           hc.cipherSuite,
		Random:                rand.Reader,
		Pattern:               pattern,
		Initiator:             initiator,
		StaticKeypair:         noise.DHKey{Private: hc.privateKey, Public: hc.Cert.PublicKey()},
		PresharedKey:          []byte{},
		PresharedKeyPlacement: 0,
	})
}
```
