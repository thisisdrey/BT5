Confirmed: after reading the issuer field at `cert/cert_v2.go:731-733`, `unmarshalDetails` returns immediately at line 735 without ever calling `b.Empty()` to verify no trailing bytes remain in the Details envelope. This matches the question's exact claim.

### Title
Missing trailing-empty check in `unmarshalDetails` allows extra unrecognized ASN.1 elements after `TagDetailsIssuer` to be silently ignored - ([File: cert/cert_v2.go])

### Summary
`unmarshalDetails` reads the `Details` envelope field-by-field (name, networks, unsafe networks, groups, isCA, notBefore, notAfter, issuer) but never checks `b.Empty()` after the issuer read, so any extra attacker-appended ASN.1 elements inside the envelope are accepted and dropped rather than rejected. `rawDetails` (used for signing/fingerprint) still contains those extra bytes, while the parsed `detailsV2` struct silently omits them, creating a discrepancy between the byte-level and struct-level views of the certificate.

### Finding Description
`unmarshalCertificateV2` extracts `rawDetails` as a whole ASN.1 element via `input.ReadASN1Element(&rawDetails, TagCertDetails)` at `cert/cert_v2.go:584`, preserving the full tag+length+content, then passes it to `unmarshalDetails(rawDetails)` at `cert/cert_v2.go:620`. Inside `unmarshalDetails`, the envelope is opened with `b.ReadASN1(&b, TagCertDetails)` (`cert/cert_v2.go:643`), and then fields are read sequentially, ending with the optional issuer read at `cert/cert_v2.go:731`:
```go
var issuer cryptobyte.String
if !b.ReadOptionalASN1(&issuer, nil, TagDetailsIssuer) {
    return detailsV2{}, ErrBadFormat
}

return detailsV2{...}, nil
```
There is no `if !b.Empty() { return detailsV2{}, ErrBadFormat }` check before the final `return`. Every other structural boundary in this codebase (`unmarshalCertificateV2`'s outer SEQUENCE check at line 578, `rawDetails.Empty()` check at line 584) explicitly enforces exhaustion, but this one is missing at the very end of `unmarshalDetails`.

An attacker who controls the DER-encoded Details substructure (fully controlled since certs are attacker-presented, e.g., in the handshake) can append arbitrary extra context-specific tagged elements after the issuer TLV inside the `TagCertDetails` envelope. `cryptobyte`'s `ReadASN1`/`ReadOptionalASN1` calls only consume the bytes for their own tag; they do not fail if there are leftover bytes afterward. Consequently, `unmarshalDetails` returns a `detailsV2` struct that reflects only the recognized fields, while `rawDetails` (which is signed over and used for `Fingerprint()`/`CheckSignature()`) contains the extra trailing bytes. This is a parser divergence: the signature covers the padded byte stream, but the semantic checks in `c.validate()` and all consumers of `detailsV2` (networks/groups used by firewall and CA-pool checks) never see or reason about the extra data.

### Impact Explanation
This is a parsing-strictness violation rather than a direct authentication or firewall bypass: the extra trailing bytes do not currently map to any field that firewall or CA-pool logic reads, so no existing check is subverted with attacker-controlled semantic content in this repo state. The primary demonstrable impact is that malformed/non-canonical certificates (with injected junk after the issuer) are accepted by parsing when they should be rejected, and `rawDetails`/`Fingerprint()` differ from what a canonical/strict parse of the same conceptual fields would produce, since `rawDetails` includes the trailing junk. This could enable certificate malleability (multiple distinct byte encodings mapping to the same "parsed" identity/fields) which is relevant to fingerprint-based CA-pool blocklisting or hash-based revocation-adjacent checks (`Fingerprint()` at `cert/cert_v2.go:129`) — two different signed byte strings could parse to identical `detailsV2` (same name/networks/groups) yet have different fingerprints, potentially evading fingerprint-based blocking.

### Likelihood Explanation
Fully attacker-controlled: any party generating or forwarding a certificate for parsing controls the raw DER bytes 100%, and no CA signature validity is required to reach this parsing path (the check happens in `unmarshalDetails` before/independent of signature verification against a trust anchor). This is trivially reproducible by constructing a Details TLV with a valid recognized prefix and appending any extra context-specific tag/bytes.

### Recommendation
Add `if !b.Empty() { return detailsV2{}, ErrBadFormat }` immediately after the issuer read in `unmarshalDetails` (before the final `return`), mirroring the strict exhaustion checks used elsewhere in `unmarshalCertificateV2`.

### Proof of Concept
Fuzz/unit test plan:
1. Construct a minimal valid `detailsV2` DER encoding (name, optional fields, notBefore/notAfter, issuer) using `detailsV2.Marshal()`.
2. Manually append extra bytes representing an arbitrary unrecognized context-specific ASN.1 TLV (e.g., tag `0x8F` with some payload) directly after the issuer TLV, inside the outer `TagCertDetails` envelope (adjust the outer length accordingly).
3. Call `unmarshalDetails` on this crafted buffer.
4. Assert: current behavior returns `(detailsV2{...}, nil)` with no error (bug confirmed) — expected/fixed behavior should return `(detailsV2{}, ErrBadFormat)`.
5. Additionally assert that `rawDetails` (as returned by `unmarshalCertificateV2`) contains the extra bytes while `detailsV2` does not reflect them, demonstrating the byte-vs-struct divergence. [1](#0-0) [2](#0-1)

### Citations

**File:** cert/cert_v2.go (L570-639)
```go
func unmarshalCertificateV2(b []byte, publicKey []byte, curve Curve) (*certificateV2, error) {
	l := len(b)
	if l == 0 || l > MaxCertificateSize {
		return nil, ErrBadFormat
	}

	input := cryptobyte.String(b)
	// Open the envelope
	if !input.ReadASN1(&input, asn1.SEQUENCE) || input.Empty() {
		return nil, ErrBadFormat
	}

	// Grab the cert details, we need to preserve the tag and length
	var rawDetails cryptobyte.String
	if !input.ReadASN1Element(&rawDetails, TagCertDetails) || rawDetails.Empty() {
		return nil, ErrBadFormat
	}

	//Maybe grab the curve
	var rawCurve byte
	if !readOptionalASN1Byte(&input, &rawCurve, TagCertCurve, byte(curve)) {
		return nil, ErrBadFormat
	}
	curve = Curve(rawCurve)

	// Maybe grab the public key
	var rawPublicKey cryptobyte.String
	if len(publicKey) > 0 {
		// If a public key is passed in, then the handshake certificate must
		// not have a public key present
		if input.PeekASN1Tag(TagCertPublicKey) {
			return nil, ErrCertPubkeyPresent
		}
		rawPublicKey = make(cryptobyte.String, len(publicKey))
		copy(rawPublicKey, publicKey)
	} else if !input.ReadOptionalASN1(&rawPublicKey, nil, TagCertPublicKey) {
		return nil, ErrBadFormat
	}

	if len(rawPublicKey) == 0 {
		return nil, ErrBadFormat
	}

	// Grab the signature
	var rawSignature cryptobyte.String
	if !input.ReadASN1(&rawSignature, TagCertSignature) || rawSignature.Empty() {
		return nil, ErrBadFormat
	}

	// Finally unmarshal the details
	details, err := unmarshalDetails(rawDetails)
	if err != nil {
		return nil, err
	}

	c := &certificateV2{
		details:    details,
		rawDetails: rawDetails,
		curve:      curve,
		publicKey:  rawPublicKey,
		signature:  rawSignature,
	}

	err = c.validate()
	if err != nil {
		return nil, err
	}

	return c, nil
}
```

**File:** cert/cert_v2.go (L641-745)
```go
func unmarshalDetails(b cryptobyte.String) (detailsV2, error) {
	// Open the envelope
	if !b.ReadASN1(&b, TagCertDetails) || b.Empty() {
		return detailsV2{}, ErrBadFormat
	}

	// Read the name
	var name cryptobyte.String
	if !b.ReadASN1(&name, TagDetailsName) || name.Empty() || len(name) > MaxNameLength {
		return detailsV2{}, ErrBadFormat
	}

	// Read the network addresses
	var subString cryptobyte.String
	var found bool

	if !b.ReadOptionalASN1(&subString, &found, TagDetailsNetworks) {
		return detailsV2{}, ErrBadFormat
	}

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

	// Read out IsCA
	var isCa bool
	if !readOptionalASN1Boolean(&b, &isCa, TagDetailsIsCA, false) {
		return detailsV2{}, ErrBadFormat
	}

	// Read not before and not after
	var notBefore int64
	if !b.ReadASN1Int64WithTag(&notBefore, TagDetailsNotBefore) {
		return detailsV2{}, ErrBadFormat
	}

	var notAfter int64
	if !b.ReadASN1Int64WithTag(&notAfter, TagDetailsNotAfter) {
		return detailsV2{}, ErrBadFormat
	}

	// Read issuer
	var issuer cryptobyte.String
	if !b.ReadOptionalASN1(&issuer, nil, TagDetailsIssuer) {
		return detailsV2{}, ErrBadFormat
	}

	return detailsV2{
		name:           string(name),
		networks:       networks,
		unsafeNetworks: unsafeNetworks,
		groups:         groups,
		isCA:           isCa,
		notBefore:      time.Unix(notBefore, 0),
		notAfter:       time.Unix(notAfter, 0),
		issuer:         hex.EncodeToString(issuer),
	}, nil
}
```
