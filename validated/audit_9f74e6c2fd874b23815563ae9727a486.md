### Title
Unauthenticated CPU amplification via unbounded certificate `networks`/`groups`/`unsafeNetworks` parsing before signature verification - (File: cert/cert_v2.go, handshake/machine.go)

### Summary
The external report describes a bug class where a cheap, attacker-controlled "commit" step (submitting an order) is allowed to specify an amount that is not bounded, so that the expensive "redemption" step (`mineGolds`, which loops over all committed IDs) can be driven to unbounded cost/gas exhaustion. The root cause is: an untrusted, attacker-supplied *count* value determines the number of iterations of an expensive per-item operation, and this count is never capped independently of the overall transaction/request size.

The closest reachable analog in Nebula is in the Noise-IX handshake certificate path: an unauthenticated peer's certificate is fully **parsed, sorted, and structurally validated before its signature is checked against a trusted CA**. The number of `networks`, `unsafeNetworks`, and `groups` entries in the ASN.1-encoded certificate `Details` is not capped by any dedicated maximum-count constant — only individual item length is capped (`MaxNetworkLength`, `MaxNameLength` for the *name* field only). This lets an unauthenticated peer force the responder to do `O(n log n)` sort work and `O(n)` validation work over thousands of attacker-chosen entries, per handshake packet, before any cryptographic trust decision is made — directly mirroring the "cheap commit, expensive uncapped processing" pattern in the reported bug.

### Finding Description
`handshake.Machine.validateCert` (`handshake/machine.go`) is invoked from `processPayload` while handling an incoming handshake message, and it calls `cert.Recombine` → `unmarshalCertificateV2`/`unmarshalDetails` (`cert/cert_v2.go`) **before** `m.verifier(rc)` (which performs CA trust/signature verification) is called: [1](#0-0) 

`unmarshalDetails` reads `networks`, `unsafeNetworks`, and `groups` as ASN.1 `SEQUENCE OF` fields with **no limit on the number of elements** — only a per-element length check (`MaxNetworkLength = 17` for networks/unsafe networks) and, separately, a name-length check that does not apply to individual group strings: [2](#0-1) 

After parsing, `certificateV2.validate()` — called from `unmarshalCertificateV2` prior to any signature check — performs `slices.SortFunc` and `findDuplicatePrefix` over the *entire* attacker-supplied `networks` and `unsafeNetworks` slices: [3](#0-2) [4](#0-3) 

The only overall size cap is the certificate-wide `MaxCertificateSize = 65536` bytes: [5](#0-4) [6](#0-5) 

Because each `networks`/`unsafeNetworks` entry only costs a few bytes (an ASN.1 OCTET STRING tag + length + up to 17 bytes of payload) and each `groups` entry only costs a tag + length + short UTF8 string, an attacker can pack several thousand entries into a single 64KB certificate blob and send it as the certificate payload of an ordinary handshake message — reachable by anyone with no CA-signed certificate, since this is exactly the code path that decides *whether* a peer will ever be trusted. This is directly analogous to the reported bug: a cheap "commit" (submit handshake packet with a maximal cert) triggers expensive, count-driven processing (`sort` + `O(n)` validate) that is not gated by any dedicated per-entry-count limit, and the gate (CA verification) is checked only *after* the expensive work is already done.

### Impact Explanation
An unauthenticated remote peer can force the responder to spend CPU parsing, allocating, and sorting thousands of `netip.Prefix`/string entries per handshake packet, for every stage-1 handshake it sends — before the certificate's signature is checked and before the peer has proven any trust relationship. Combined with the ability to send many such packets (each triggering a new `handshake.Machine` and full parse/sort cycle in `beginHandshake`), this constitutes a remote CPU-exhaustion / amplification vector against the handshake responder path, i.e., a remote resource-exhaustion impact matching the "remote crash/DoS" impact category for this class of finding. It does not by itself grant certificate/authentication bypass; the impact is availability degradation of the handshake responder.

### Likelihood Explanation
Likelihood is limited by the size cap on individual items (17 bytes for networks, `MaxNameLength` only for the top-level `name`, not for `groups` entries) and the overall 64KB certificate cap, so per-packet cost is bounded (roughly thousands, not millions, of entries), and the resulting `O(n log n)` sort is not catastrophic on its own. However, since this cost is paid on every stage-1 handshake packet, from any address (subject only to `AllowUnknownVpnAddr`), and prior to any credential-based rate limiting, a sustained flood of maximal-size crafted certs is straightforward for an attacker to construct and repeat.

### Recommendation
Add explicit maximum-count constants for `networks`, `unsafeNetworks`, and `groups` (analogous to `MaxNetworkLength`/`MaxNameLength`) and enforce them in `unmarshalDetails` (`cert/cert_v2.go`) at parse time, rejecting certificates that exceed the count before any per-item work (sorting, duplicate detection) is performed. Consider also capping the number of concurrent handshake Machines / cert-parses done per unauthenticated source, and/or performing certificate size-based rate limiting earlier in `handshake_manager.go`'s `beginHandshake`/`HandleIncoming` path.

### Proof of Concept
1. Craft a Nebula v2 certificate whose `Details` ASN.1 sequence contains, e.g., ~3,800 `networks` OCTET STRING entries (17 bytes each) and/or several thousand `groups` UTF8String entries, filling the certificate up to the `MaxCertificateSize` (65536 byte) limit, with an arbitrary (invalid) signature/public key.
2. Embed this oversized `Details` blob as the `Cert` field of a handshake `Payload` and send it as the stage-1 packet of a Noise-IX handshake to a Nebula node (`handshake.Machine.ProcessPacket` → `processPayload` → `validateCert` → `cert.Recombine`/`unmarshalCertificateV2`).
3. Observe that `unmarshalDetails` parses all entries and `certificateV2.validate()` sorts/deduplicates the full `networks`/`unsafeNetworks` slices *before* `m.verifier(rc)` rejects the certificate for signature/CA mismatch — i.e., the expensive work is fully paid regardless of certificate validity.
4. Repeat with many source addresses/handshake attempts to demonstrate cumulative CPU cost on the responder, mirroring the "cheap commit, unbounded expensive processing" pattern from the source report.

### Citations

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

**File:** cert/cert_v2.go (L42-52)
```go
const (
	// MaxCertificateSize is the maximum length a valid certificate can be
	MaxCertificateSize = 65536

	// MaxNameLength is limited to a maximum realistic DNS domain name to help facilitate DNS systems
	MaxNameLength = 253

	// MaxNetworkLength is the maximum length a network value can be.
	// 16 bytes for an ipv6 address + 1 byte for the prefix length
	MaxNetworkLength = 17
)
```

**File:** cert/cert_v2.go (L391-460)
```go
func (c *certificateV2) validate() error {
	// Empty names are allowed

	if len(c.publicKey) == 0 {
		return ErrInvalidPublicKey
	}

	if !c.details.isCA && len(c.details.networks) == 0 {
		return NewErrInvalidCertificateProperties("non-CA certificate must contain at least 1 network")
	}

	hasV4Networks := false
	hasV6Networks := false
	for _, network := range c.details.networks {
		if !network.IsValid() || !network.Addr().IsValid() {
			return NewErrInvalidCertificateProperties("invalid network: %s", network)
		}

		if network.Addr().IsUnspecified() {
			return NewErrInvalidCertificateProperties("non-CA certificates must not use the zero address as a network: %s", network)
		}

		if network.Addr().Zone() != "" {
			return NewErrInvalidCertificateProperties("networks may not contain zones: %s", network)
		}

		if network.Addr().Is4In6() {
			return NewErrInvalidCertificateProperties("4in6 networks are not allowed: %s", network)
		}

		hasV4Networks = hasV4Networks || network.Addr().Is4()
		hasV6Networks = hasV6Networks || network.Addr().Is6()
	}

	slices.SortFunc(c.details.networks, comparePrefix)
	err := findDuplicatePrefix(c.details.networks)
	if err != nil {
		return err
	}

	for _, network := range c.details.unsafeNetworks {
		if !network.IsValid() || !network.Addr().IsValid() {
			return NewErrInvalidCertificateProperties("invalid unsafe network: %s", network)
		}

		if network.Addr().Zone() != "" {
			return NewErrInvalidCertificateProperties("unsafe networks may not contain zones: %s", network)
		}

		if !c.details.isCA {
			if network.Addr().Is6() {
				if !hasV6Networks {
					return NewErrInvalidCertificateProperties("IPv6 unsafe networks require an IPv6 address assignment: %s", network)
				}
			} else if network.Addr().Is4() {
				if !hasV4Networks {
					return NewErrInvalidCertificateProperties("IPv4 unsafe networks require an IPv4 address assignment: %s", network)
				}
			}
		}
	}

	slices.SortFunc(c.details.unsafeNetworks, comparePrefix)
	err = findDuplicatePrefix(c.details.unsafeNetworks)
	if err != nil {
		return err
	}

	return nil
}
```

**File:** cert/cert_v2.go (L570-574)
```go
func unmarshalCertificateV2(b []byte, publicKey []byte, curve Curve) (*certificateV2, error) {
	l := len(b)
	if l == 0 || l > MaxCertificateSize {
		return nil, ErrBadFormat
	}
```

**File:** cert/cert_v2.go (L641-710)
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
```

**File:** cert/sign.go (L159-170)
```go
// findDuplicatePrefix returns an error if there is a duplicate prefix in the pre-sorted input slice sortedPrefixes
func findDuplicatePrefix(sortedPrefixes []netip.Prefix) error {
	if len(sortedPrefixes) < 2 {
		return nil
	}
	for i := 1; i < len(sortedPrefixes); i++ {
		if comparePrefix(sortedPrefixes[i], sortedPrefixes[i-1]) == 0 {
			return NewErrInvalidCertificateProperties("duplicate network detected: %v", sortedPrefixes[i])
		}
	}
	return nil
}
```
