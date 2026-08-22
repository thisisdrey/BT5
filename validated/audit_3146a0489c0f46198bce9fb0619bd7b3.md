Confirmed: `Machine.validateCert` calls `cert.Recombine` → `unmarshalCertificateV2`/`unmarshalDetails` on every stage-1 handshake packet, *before* `m.verifier(rc)` (CA-pool signature verification) is invoked. This means the certificate's `groups` field is fully parsed pre-authentication, by any unauthenticated remote peer with no CA-signed certificate.

### Title
Unbounded/duplicate-tolerant `groups` field in V2 certificate parsing enables pre-auth handshake CPU/memory amplification - (File: `cert/cert_v2.go`)

### Summary
Nebula's certificate `Details.groups` field (ASN.1 `SEQUENCE OF Name OPTIONAL`, per `cert/cert_v2.asn1`) is parsed by `unmarshalDetails` with no limit on the number of entries and no duplicate check, unlike the adjacent `networks`/`unsafeNetworks` fields which are explicitly bounded, sorted, and de-duplicated via `findDuplicatePrefix`. Because certificate parsing (`cert.Recombine` → `unmarshalCertificateV2`) happens inside `handshake.Machine.validateCert` *before* the CA-pool signature check (`m.verifier(rc)`), any unauthenticated peer sending a stage-1 handshake packet can force the responder to allocate and iterate over a large number of (possibly duplicate) group strings, with no certificate signature required at all.

### Finding Description
`unmarshalDetails` in `cert/cert_v2.go` (lines ~697-710) reads the `groups` sequence: [1](#0-0) 
Each entry only requires a non-empty UTF8String — there is no `MaxNameLength`-style bound (contrast with the `name` field check at `cert/cert_v2.go:649`) and no cap on the total count of groups, and no de-duplication, unlike `networks`/`unsafeNetworks` handling in `validate()`: [2](#0-1) 

This parsing runs inside `unmarshalCertificateV2`, invoked from `cert.Recombine`: [3](#0-2) 

which is called from `Machine.validateCert` for every certificate-bearing handshake message, strictly *before* `m.verifier(rc)` performs any CA/signature validation: [4](#0-3) 

`HandshakeManager.beginHandshake` runs this path for every unsolicited stage-1 packet from an unauthenticated remote address (subject only to a remote-allow-list IP check, not to certificate validity): [5](#0-4) [6](#0-5) 

This is directly analogous to the reported `selected_source_types` bug class: an attacker-supplied vector (here, ASN.1 `groups`) has no length cap and permits duplicates, and this cost is not accounted for prior to expensive processing — except here the exposure is worse, since it occurs with zero attacker-side authentication (no valid CA-signed certificate needed at all) and prior to any cryptographic verification of the certificate.

### Impact Explanation
While the total certificate size is capped at `MaxCertificateSize = 65536` bytes, an attacker can still pack thousands of short, duplicate group-name entries into that budget. Each such certificate forces:
- ASN.1 sub-parsing of every group entry (`unmarshalDetails`) on every handshake attempt, before any signature check,
- allocation of a `[]string` sized to the crafted count.

Because no signature check occurs first, an attacker can generate an unlimited stream of unique such packets, each with no marginal cost to the attacker beyond generating a new self-signed-looking (invalid) certificate blob, but each imposing parsing/allocation cost on the responder for every stage-1 handshake packet received. This is a low-cost, remotely reachable CPU/memory amplification vector against the handshake responder path, consistent with the reported bug class of unbounded/duplicate-tolerant vectors enabling network spam / DoS.

### Likelihood Explanation
High. The attack requires no valid CA-signed certificate, no established session, and no allow-listed identity beyond passing the coarse `RemoteAllowList.AllowUnknownVpnAddr` IP check. It only requires sending well-formed-enough stage-1 handshake UDP packets with a crafted V2 certificate payload, which is directly reachable by any network-adjacent, unauthenticated attacker.

### Recommendation
- Add an explicit maximum count for `groups` entries in `unmarshalDetails` (analogous to `MaxNetworkLength`/`MaxNameLength` bounds already used for `networks`/`name`).
- Reject certificates containing duplicate group names during parsing/`validate()`, mirroring the `findDuplicatePrefix` treatment already applied to `networks` and `unsafeNetworks`.
- Consider bounding parse-time cost (e.g., limiting total entries processed) independent of the overall `MaxCertificateSize`, since the current 65536-byte cap still permits thousands of tiny/duplicate group entries.

### Proof of Concept
1. Craft a V2 host certificate whose ASN.1 `Details.groups` field contains, e.g., 5,000 duplicate 1-byte group names (well within `MaxCertificateSize`).
2. Send this certificate as the `Cert` field of a stage-1 `HandshakeIXPSK0` packet to a victim Nebula node's UDP listener, from any IP not on the node's remote deny list.
3. Observe that `HandshakeManager.beginHandshake` → `Machine.ProcessPacket` → `validateCert` → `cert.Recombine` → `unmarshalCertificateV2`/`unmarshalDetails` fully parses and allocates the 5,000-entry `groups` slice *before* `m.verifier` rejects the certificate for lacking a valid CA signature — i.e., the parsing cost is paid regardless of certificate validity.
4. Repeating this with many packets (each a fresh, cheaply-generated malformed certificate) forces repeated parsing/allocation work on the responder with no corresponding cost or authentication on the attacker's side.

**Note on limitations:** I could not execute the PoC or directly measure the CPU/allocation cost of this parsing path in this environment; the analysis is based on static code tracing of `cert/cert_v2.go`, `cert/cert.go`, `handshake/machine.go`, and `handshake_manager.go`. I also could not fully verify whether any upstream rate-limiting (e.g., per-source-IP handshake throttling) exists elsewhere in the handshake manager that might partially mitigate this; I did not find such throttling in the reviewed code, but the surrounding files were not exhaustively covered given index limits, and a Devin session with full file access would be needed to confirm no additional per-IP handshake-rate gating exists before `beginHandshake` is reached.

### Citations

**File:** cert/cert_v2.go (L391-429)
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
```

**File:** cert/cert_v2.go (L697-710)
```go
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

**File:** cert/cert.go (L124-159)
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

	if c.Curve() != curve {
		return nil, fmt.Errorf("certificate curve %s does not match expected %s", c.Curve().String(), curve.String())
	}

	return c, nil
}
```

**File:** handshake/machine.go (L342-380)
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
}
```

**File:** handshake_manager.go (L151-195)
```go
func (hm *HandshakeManager) HandleIncoming(via ViaSender, packet []byte, h *header.H) {
	// Gate on known handshake subtypes. Unknown subtypes (or future ones we
	// don't yet support) are dropped here rather than silently routed through
	// the IX path. Add a case when introducing a new pattern.
	switch h.Subtype {
	case header.HandshakeIXPSK0:
		// supported
	default:
		hm.l.Debug("dropping handshake with unsupported subtype",
			"from", via, "subtype", h.Subtype)
		return
	}

	// First remote allow list check before we know the vpnIp
	if !via.IsRelayed {
		if !hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(via.UdpAddr.Addr()) {
			hm.l.Debug("lighthouse.remote_allow_list denied incoming handshake", "from", via)
			return
		}
	}

	// First message of a new handshake. The wire format requires RemoteIndex
	// to be zero here (the initiator has no responder index to fill in yet),
	// and generateIndex never allocates 0, so any non-zero RemoteIndex on a
	// stage-1 packet is malformed or someone probing for an index collision.
	// Drop without paying the cost of running noise on a pending Machine.
	if h.MessageCounter == 1 {
		if h.RemoteIndex != 0 {
			hm.l.Debug("dropping stage-1 handshake with non-zero RemoteIndex",
				"from", via, "remoteIndex", h.RemoteIndex)
			return
		}
		hm.beginHandshake(via, packet, h)
		return
	}

	// Continuation message must match a pending handshake by index.
	// Anything else is an orphaned packet (e.g., late retransmit after
	// timeout) and is dropped.
	if hh := hm.queryIndex(h.RemoteIndex); hh != nil {
		hm.continueHandshake(via, hh, packet)
		return
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
