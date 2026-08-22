### Title
Unbounded array allocation from unauthenticated handshake certificate parsing enables per-packet CPU/memory amplification (`checkCAConstraints` quadratic cost) - ([File: cert/cert_v2.go], [File: cert/ca_pool.go])

### Summary
`unmarshalDetails` in `cert/cert_v2.go` builds the `networks`, `unsafeNetworks`, and `groups` slices of a certificate purely from attacker-supplied bytes, with no cap on the *number* of elements — only a per-element size cap (`MaxNetworkLength`) and an overall byte cap (`MaxCertificateSize = 65536`) apply. This parsing happens in `handshake.Machine.validateCert` → `cert.Recombine` → `unmarshalCertificateV2`, i.e. **before** any CA signature check (`ncp.verify`/`CheckSignature`) is performed, so a peer with no CA-signed certificate at all can trigger it just by sending a crafted stage-0/stage-1 handshake packet.

### Finding Description
`unmarshalDetails` (cert/cert_v2.go:641-745) loops `for !subString.Empty() { ... }` three separate times to populate `networks`, `unsafeNetworks`, and `groups`, appending one element per iteration: [1](#0-0) [2](#0-1) 

Each network entry only needs to be ≥1 byte (rejected only if `len(val) > MaxNetworkLength`, i.e. 17), and each group string only needs to be non-empty. Since the overall input is capped at `MaxCertificateSize = 65536` bytes: [3](#0-2) 

an attacker can pack thousands of minimal-size network/group entries into a single unauthenticated certificate blob. This is reached from the handshake path — `Machine.validateCert` calls `cert.Recombine`, which calls `unmarshalCertificateV2`, *before* `m.verifier(rc)` (the CA-pool signature check) is invoked: [4](#0-3) 

Once parsed, `certificateV2.validate()` iterates the resulting slices, sorts them (`slices.SortFunc`, O(n log n)) and checks for duplicates: [5](#0-4) 

If the cert manages to reach `CheckCAConstraints` (e.g., a self-consistent but forged cert, or any cert whose issuer field happens to match a configured CA fingerprint before the final `CheckSignature` short-circuits), `checkCAConstraints` performs a nested double loop over `networks × signingNetworks` and `unsafeNetworks × signingUnsafeNetworks`: [6](#0-5) 

This is the direct structural analog of the reported "gas overflow" bug: an array whose length is controlled by an untrusted/unauthenticated party with no upper bound on element *count*, only on element or total size — matching the report's exact recommendation ("add a comment that no significant number is expected, or add limit/offset parameters").

### Impact Explanation
Because this code executes prior to CA-signature verification, a remote UDP peer needs **no valid certificate at all** to reach it — they simply craft raw ASN.1 bytes for the `Cert` field of a handshake payload. This allows CPU amplification per handshake packet (parsing + sorting + duplicate-detection over thousands of entries), which can be repeated across many concurrent/rapid handshake attempts from spoofed or real UDP sources, contributing to a remote CPU-based denial of service against the nebula listener process. It does not by itself bypass authentication or decrypt traffic, but it fits the accepted "remote crash/DoS" impact class from the validation criteria.

### Likelihood Explanation
High reachability: any UDP sender can transmit a handshake stage-0 packet (`header.Handshake`) to a nebula node's listen port; `HandshakeManager.beginHandshake` constructs a responder `Machine` and calls `ProcessPacket`, which unconditionally reaches `validateCert`/`Recombine` for the cert payload before verification: [7](#0-6) 
No prior trust relationship, valid certificate, or CA membership is required to reach the parsing/sorting code — only a well-formed enough ASN.1 envelope to pass `ReadASN1`.

### Recommendation
Add an explicit maximum element count for `networks`, `unsafeNetworks`, and `groups` in `unmarshalDetails` (and the equivalent v1 IP/subnet pair loops in `cert/cert_v1.go`), analogous to the existing `MaxNetworkLength`/`MaxNameLength` constants — e.g. reject certificates whose network, unsafe-network, or group counts exceed a small fixed bound (tens, not thousands) before allocating/sorting the slices. This bounds the per-packet CPU cost of parsing an unauthenticated handshake certificate independent of the 64 KB total-size cap.

### Proof of Concept
1. Craft a minimal ASN.1 `certificateV2` envelope (`SEQUENCE` containing `TagCertDetails`, `TagCertPublicKey`, `TagCertSignature`) whose `TagDetailsNetworks` field contains ~3,800 back-to-back 1-byte-payload `OCTET_STRING` entries (staying under `MaxCertificateSize` = 65536 bytes) and/or `TagDetailsGroups` containing thousands of 1-byte `UTF8String` entries.
2. Embed this blob as the `Cert` field of a handshake `NebulaHandshakeDetails` payload (`handshake/payload.go` `fieldCert`), wrap it as the Noise handshake message body, and send it as a stage-0 handshake packet (`header.Handshake`, subtype `HandshakeIXPSK0`) to a nebula node's UDP listen port — no CA, no valid signature, no prior tunnel required.
3. Observe that `HandshakeManager.beginHandshake` → `Machine.ProcessPacket` → `validateCert` → `cert.Recombine` → `unmarshalCertificateV2`/`unmarshalDetails` allocates and appends thousands of slice elements, then `certificateV2.validate()` sorts and duplicate-checks them — all executed before `CheckSignature`/`CheckCAConstraints` can reject the forged certificate. Repeating this from many source ports/spoofed addresses amplifies CPU cost on the target far beyond the cost of generating the packet.

### Citations

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

**File:** cert/cert_v2.go (L391-457)
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

**File:** cert/cert_v2.go (L698-710)
```go
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

**File:** cert/ca_pool.go (L308-342)
```go
	// If the signer has a limited set of ip ranges to issue from make sure the cert only contains a subset
	signingNetworks := signer.Networks()
	if len(signingNetworks) > 0 {
		for _, certNetwork := range networks {
			found := false
			for _, signingNetwork := range signingNetworks {
				if signingNetwork.Contains(certNetwork.Addr()) && signingNetwork.Bits() <= certNetwork.Bits() {
					found = true
					break
				}
			}

			if !found {
				return fmt.Errorf("certificate contained a network assignment outside the limitations of the signing ca: %s", certNetwork.String())
			}
		}
	}

	// If the signer has a limited set of subnet ranges to issue from make sure the cert only contains a subset
	signingUnsafeNetworks := signer.UnsafeNetworks()
	if len(signingUnsafeNetworks) > 0 {
		for _, certUnsafeNetwork := range unsafeNetworks {
			found := false
			for _, caNetwork := range signingUnsafeNetworks {
				if caNetwork.Contains(certUnsafeNetwork.Addr()) && caNetwork.Bits() <= certUnsafeNetwork.Bits() {
					found = true
					break
				}
			}

			if !found {
				return fmt.Errorf("certificate contained an unsafe network assignment outside the limitations of the signing ca: %s", certUnsafeNetwork.String())
			}
		}
	}
```

**File:** handshake_manager.go (L701-726)
```go
func (hm *HandshakeManager) beginHandshake(via ViaSender, packet []byte, h *header.H) {
	f := hm.f
	cs := f.pki.getCertState()

	v := cs.DefaultVersion()
	if cs.GetCredential(v) == nil {
		f.l.Error("Unable to handshake with host because no certificate is available",
			"from", via, "certVersion", v)
		return
	}

	machine, err := handshake.NewMachine(
		v, cs.GetCredential,
		hm.certVerifier(), func() (uint32, error) { return generateIndex(f.l) },
		false, header.HandshakeIXPSK0,
	)
	if err != nil {
		f.l.Error("Failed to create handshake machine", "from", via, "error", err)
		return
	}

	response, result, err := machine.ProcessPacket(nil, packet)
	if err != nil {
		f.l.Error("Failed to process handshake packet", "from", via, "error", err)
		return
	}
```
