### Title
Unauthenticated handshake initiation forces expensive Noise DH computation on the responder with no anti-DoS cost gate - (File: `handshake_manager.go`, `handshake/machine.go`, `handshake/handshake.proto`)

### Summary
The H-17 report describes a poster paying a trivial, unvalidated fee (`gas_price = 1`) to force validators to perform disproportionately expensive work (Tally program execution at a high gas limit), because `post_request()` never enforces a minimum cost for the resource consumed. The reachable Nebula analog is `HandshakeManager.HandleIncoming`/`beginHandshake`, which processes any stage-1 handshake packet by running a full Noise `ReadMessage` (elliptic-curve DH) before any certificate/authentication check succeeds, while the one anti-DoS mechanism ever designed for this path (the `Cookie` field) was never implemented.

### Finding Description
When an inbound UDP packet has `MessageCounter == 1` and `RemoteIndex == 0`, `HandleIncoming` unconditionally calls `beginHandshake`, which builds a fresh Noise `HandshakeState` and calls `machine.ProcessPacket`, which in turn calls `m.hs.ReadMessage(...)` [1](#0-0) . `ReadMessage` performs the Noise DH exchange (an X25519 point multiplication against the sender-supplied ephemeral key and our static key) before any certificate or payload content is validated — validation of the peer certificate only happens afterward, inside `processPayload`/`validateCert`, and only if `ReadMessage` succeeds [2](#0-1) [3](#0-2) .

The only pre-checks performed before this expensive cryptographic operation are: subtype gating, a remote-allow-list check (skipped entirely for relayed traffic), and a check that `RemoteIndex == 0` [4](#0-3) . None of these require the sender to have expended any comparable cost, and none amount to a "minimum cost" gate analogous to a minimum `gas_price`.

Critically, the wire format documents that a `Cookie` field was reserved specifically as an anti-DoS mechanism, but it was never implemented in any released version, and the hand-written parser silently skips it on read: "Cookie was reserved for an anti-DoS mechanism that was never implemented. No released version of nebula has ever populated it; the hand-written parser silently skips it on read." [5](#0-4) . This is a direct structural analog to the SEDA bug: a designed cost/anti-abuse mechanism exists in the protocol shape but is not enforced, so an attacker pays essentially nothing (one UDP packet with a well-formed header and an arbitrary ephemeral key) to force the responder to spend non-trivial CPU (asymmetric crypto) — a resource-drain-for-minimal-cost pattern, just as in H-17 where the poster pays a minimal fee (`gas_price=1`) to force validators into expensive Tally execution.

### Impact Explanation
An attacker with no valid certificate and no established relationship with the target can flood stage-1 handshake packets (any 4-byte-plus header shaped to pass `header.Len` and subtype checks, with `RemoteIndex=0`), forcing the responder to allocate a new `noise.HandshakeState` and perform an elliptic-curve DH computation for each one, before any authentication succeeds. Because the DH cost is paid on the responder side regardless of whether the certificate later fails validation, a sustained flood can consume significant CPU on the responder, degrading its ability to service legitimate peers — a remote resource-exhaustion / DoS impact, consistent with "causing chain delays or halts" in the original finding's severity class, translated to "causing tunnel-establishment delays or service degradation" for Nebula nodes.

### Likelihood Explanation
Likelihood is high for anyone with an accepted underlay path to a lighthouse or node: the remote-allow-list check is the only underlay-address gate, and it is skipped entirely for relayed traffic (`via.IsRelayed`) [6](#0-5) . No certificate, PSK, or proof-of-work is required to trigger the expensive path — only a syntactically valid header. This requires no CA-signed certificate and no prior trust relationship, matching the reachability constraint for this analysis.

### Recommendation
Implement the anti-DoS cost mechanism the protocol already reserves a field for (the `Cookie`/stateless-retry style challenge) so that a responder issues a cheap, stateless challenge before committing to the expensive Noise DH step, and only performs `ReadMessage` after the initiator echoes back a valid cookie proving it received a real response from this responder. Alternatively/additionally, add per-source-address rate limiting on `beginHandshake` invocation so that DH computations are bounded regardless of how many stage-1 packets a single (possibly spoofed) source can send.

### Proof of Concept
Not executed (no runtime access in this environment). Conceptually: send repeated crafted UDP datagrams to a Nebula listener with `header.Type = Handshake`, `header.Subtype = HandshakeIXPSK0`, `MessageCounter = 1`, `RemoteIndex = 0`, and a payload containing an arbitrary/garbage 32-byte "ephemeral key" long enough to satisfy `len(packet) >= header.Len`. Each such packet reaches `beginHandshake` → `handshake.NewMachine` → `machine.ProcessPacket` → `noise.HandshakeState.ReadMessage`, which performs a DH computation using the attacker-supplied bytes before certificate validation can reject it, as shown in `handshake_manager.go:151-185` and `handshake/machine.go:223-240`.

### Citations

**File:** handshake_manager.go (L151-185)
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
```

**File:** handshake/machine.go (L223-240)
```go
	// The (eKey, dKey) ordering here is correct for IX, where the initiator
	// completes the handshake by reading the responder's stage-2 message.
	// noise returns (cs1, cs2) where cs1 is the initiator->responder cipher.
	// For 3-message patterns where a responder finishes by reading the final
	// message, this ordering would be wrong; revisit when XX/pqIX lands.
	msg, eKey, dKey, err := m.hs.ReadMessage(nil, packet[header.Len:])
	if err != nil {
		// Noise ReadMessage failed. The noise library checkpoints and rolls back
		// on failure, so the Machine is still alive. The caller can retry with
		// a different packet.
		return nil, nil, fmt.Errorf("noise ReadMessage: %w", err)
	}

	// From here on, noise state has advanced. Any error is fatal.
	flags := m.peerMsgFlags()

	if err := m.processPayload(msg, flags); err != nil {
		return nil, nil, err
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

**File:** handshake/handshake.proto (L17-24)
```text
message NebulaHandshakeDetails {
  bytes Cert = 1;
  uint32 InitiatorIndex = 2;
  uint32 ResponderIndex = 3;
  // Cookie was reserved for an anti-DoS mechanism that was never
  // implemented. No released version of nebula has ever populated it; the
  // hand-written parser silently skips it on read.
  uint64 Cookie = 4 [deprecated = true];
```
