I have sufficient evidence to write the analog finding now.

## Title
Unauthenticated Handshake Initiation Forces Expensive Noise/ECDH Computation Before Any Proof-of-Work or Cost Validation, Enabling CPU-Exhaustion DoS - ([File: handshake_manager.go])

### Summary
Nebula's responder path processes every stage-1 handshake packet by unconditionally running a full Noise `IX` handshake step (including elliptic-curve Diffie-Hellman operations) before any costly-resource check is performed on the sender. The protocol has a `Cookie` field explicitly reserved for anti-DoS purposes, but it was never implemented, so there is no mechanism to make a peer "pay" (via a lightweight puzzle/cookie) before the responder commits CPU to the expensive cryptographic work. This mirrors the ORDH-1 bug class: a cheap, attacker-controlled input (`executionFee = 0` orders / here, a bare stage-1 UDP packet) is accepted and forwarded into an expensive downstream operation (keeper execution attempt / here, Noise handshake ECDH) that is only *then* found invalid, wasting the victim's compute resources at negligible attacker cost.

### Finding Description
`HandshakeManager.HandleIncoming` performs only cheap header checks — subtype match, an IP-based remote allow list, and `RemoteIndex == 0` — before unconditionally calling `beginHandshake` for any stage-1 packet: [1](#0-0) 

`beginHandshake` then constructs a fresh `handshake.Machine` and calls `ProcessPacket`, which performs `m.hs.ReadMessage(...)`, the Noise-protocol read that executes the actual X25519 ECDH computation, *before* any certificate/identity validation occurs: [2](#0-1) [3](#0-2) 

Certificate verification (`validateCert`/`certVerifier`) only happens afterward, inside `processPayload`, once the expensive DH has already been executed: [4](#0-3) 

The protocol format explicitly documents that a cheap anti-DoS mechanism (`Cookie`) was designed but never implemented: [5](#0-4) 

This is the same shape of bug as ORDH-1: cost-relevant validation (identity/authenticity, which determines whether the expensive work was worth doing) is deferred to *after* the expensive operation instead of being checked cheaply up front.

### Impact Explanation
Any peer able to reach the UDP listener and pass the coarse IP-based `remote_allow_list` (which is not a per-identity or rate-based check) can flood arbitrarily many garbage stage-1 packets. Each one forces the responder to allocate a `handshake.Machine` and perform a full elliptic-curve DH computation, before the packet is ultimately discarded for lacking a valid certificate. This wastes the responder's CPU on expensive asymmetric cryptography at effectively zero cost to the attacker (just a crafted UDP packet), which can degrade or deny legitimate handshake processing — a remote CPU-exhaustion/DoS impact.

### Likelihood Explanation
Any node whose IP is not excluded by `remote_allow_list` (the default configuration has no allow list restricting handshake senders) can trigger this at line-rate by sending trivially-crafted stage-1 packets (subtype `HandshakeIXPSK0`, `RemoteIndex=0`), making this straightforward to exploit remotely without any valid certificate or prior trust relationship.

### Recommendation
Reintroduce or design a lightweight, cheap-to-verify anti-DoS gate (e.g., the reserved `Cookie` mechanism, a stateless HMAC-based cookie challenge, or per-source rate limiting) that must be satisfied *before* the responder commits to the expensive Noise `ReadMessage`/ECDH step in `beginHandshake`, analogous to validating `executionFee` at order-creation time rather than at expensive execution time.

### Proof of Concept
1. An attacker (or any host allowed by the coarse `remote_allow_list`) crafts a minimal valid Nebula header: `Type=Handshake`, `Subtype=HandshakeIXPSK0`, `MessageCounter=1`, `RemoteIndex=0`, followed by arbitrary/garbage Noise payload bytes.
2. Send a high volume of such packets to the victim's UDP listener from spoofed or varied source addresses/ports.
3. Each packet passes the cheap checks in `HandleIncoming` and reaches `beginHandshake`, which builds a `handshake.Machine` and calls `ProcessPacket` → `m.hs.ReadMessage`, forcing an ECDH computation per packet.
4. Because no cookie/proof-of-work/rate-limit precedes this step, the victim's CPU is consumed proportionally to the attacker's (cheap) packet volume, while the attacker incurs negligible cost — directly analogous to sending zero-`executionFee` orders that waste keeper gas on every failed execution attempt.

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

**File:** handshake/machine.go (L203-234)
```go
func (m *Machine) ProcessPacket(out, packet []byte) ([]byte, *Result, error) {
	if m.failed {
		return nil, nil, ErrMachineFailed
	}
	if len(packet) < header.Len {
		return nil, nil, ErrPacketTooShort
	}
	// Reject packets whose subtype doesn't match the one this Machine was
	// built for. A pending handshake that suddenly receives a different
	// subtype on its index is either a stray packet that matched by chance
	// or a peer protocol violation; drop it without failing the Machine so
	// the legitimate retransmit can still complete.
	if header.MessageSubType(packet[1]) != m.subtype {
		return nil, nil, ErrSubtypeMismatch
	}
	if m.result.Initiator && m.hs.MessageIndex() == 0 {
		m.failed = true
		return nil, nil, ErrInitiateNotCalled
	}

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
```

**File:** handshake/machine.go (L285-337)
```go
func (m *Machine) processPayload(msg []byte, flags msgFlags) error {
	if len(msg) == 0 {
		if flags.expectsPayload || flags.expectsCert {
			m.failed = true
			return ErrMissingContent
		}
		return nil
	}

	payload, err := UnmarshalPayload(msg)
	if err != nil {
		m.failed = true
		return fmt.Errorf("unmarshal handshake: %w", err)
	}

	// Assert the payload contains exactly what we expect
	hasPayloadData := payload.InitiatorIndex != 0 || payload.ResponderIndex != 0 || payload.Time != 0
	if hasPayloadData != flags.expectsPayload {
		m.failed = true
		return ErrUnexpectedContent
	}

	hasCertData := len(payload.Cert) > 0
	if hasCertData != flags.expectsCert {
		m.failed = true
		return ErrUnexpectedContent
	}

	// Process payload
	if flags.expectsPayload {
		var remoteIndex uint32
		if m.result.Initiator {
			remoteIndex = payload.ResponderIndex
		} else {
			remoteIndex = payload.InitiatorIndex
		}
		// The payload presence check above can be satisfied by Time alone, so a payload
		// could still carry a zero index here. We need to reject it.
		if remoteIndex == 0 {
			m.failed = true
			return ErrInvalidRemoteIndex
		}
		m.result.RemoteIndex = remoteIndex
		m.result.HandshakeTime = payload.Time
		m.payloadSet = true
	}

	// Process certificate
	if flags.expectsCert {
		if err := m.validateCert(payload); err != nil {
			return err
		}
	}
```

**File:** handshake/handshake.proto (L17-28)
```text
message NebulaHandshakeDetails {
  bytes Cert = 1;
  uint32 InitiatorIndex = 2;
  uint32 ResponderIndex = 3;
  // Cookie was reserved for an anti-DoS mechanism that was never
  // implemented. No released version of nebula has ever populated it; the
  // hand-written parser silently skips it on read.
  uint64 Cookie = 4 [deprecated = true];
  uint64 Time = 5;
  uint32 CertVersion = 8;
  // reserved for WIP multiport
  reserved 6, 7;
```
