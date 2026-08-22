No rate limiting mechanism exists anywhere in the handshake path, confirming the finding.

### Title
Unauthenticated handshake spam forces costly Noise DH computation on the responder before any certificate/authentication check, enabling a free CPU-exhaustion DoS analog to the "no-cost spam" report - (`File: handshake_manager.go`, `handshake/machine.go`)

### Summary
`SponsorPaymaster.balances` drainage arises because `UserOp` senders pay nothing while a victim account foots the gas bill — the operation has no cost to the attacker but real cost to the victim. In Nebula, the reachable analog is the handshake responder path: any unauthenticated UDP sender can force a target node to perform expensive asymmetric cryptography (X25519 Diffie-Hellman via Noise `ReadMessage`) and per-packet index/state allocation *before* the peer's certificate is validated, and there is no cost, rate limit, or anti-DoS cookie to make the attacker pay for that resource consumption.

### Finding Description
Incoming stage-1 handshake packets are dispatched by `HandshakeManager.HandleIncoming`, which only checks the subtype and that `RemoteIndex == 0` before calling `beginHandshake` for any packet claiming to start a new handshake — no certificate or authentication check has occurred yet at this point. [1](#0-0) 

`beginHandshake` then constructs a fresh `handshake.Machine` and immediately calls `machine.ProcessPacket(nil, packet)` for every such packet, from any source, before the remote certificate is known to be valid. [2](#0-1) 

Inside `ProcessPacket`, the Noise `ReadMessage` call — which performs the elliptic-curve Diffie-Hellman computation — executes unconditionally on any well-formed packet of the correct subtype; certificate parsing/validation (`processPayload`, `requireComplete`) only happens *after* this expensive crypto step, meaning the costly operation is paid for by the responder regardless of whether the sender holds a CA-signed certificate. [3](#0-2) 

The wire format itself documents that an anti-DoS mitigation was designed but never shipped: the `Cookie` field in `NebulaHandshakeDetails` was "reserved for an anti-DoS mechanism that was never implemented," and "no released version of nebula has ever populated it." [4](#0-3) 

No rate limiter, token bucket, or per-source throttle exists anywhere in the handshake dispatch path — a search of the codebase for rate-limiting constructs on handshake handling returns no results, confirming the intended cookie-based mitigation was never built. This mirrors the report's core defect: the operation (here, triggering a DH computation) has zero cost for the initiator while imposing real, uncompensated resource cost on the counterparty, and — just as the report notes an attacker can target *other* KintoWallets — an attacker here can target any reachable Nebula node's UDP listener with crafted stage-1 packets, since only a `RemoteIndex == 0` check gates entry into the costly path, with no proof-of-work, cookie, or authentication required first.

### Impact Explanation
An attacker with no valid certificate and no relationship to the target network can send a stream of minimal UDP packets (correct header, `RemoteIndex = 0`, `MessageCounter = 1`, subtype `HandshakeIXPSK0`) to force the target to repeatedly execute Curve25519 DH operations and allocate `Machine`/`HandshakeHostInfo` state per packet, at a fraction of the attacker's own cost. Sustained at scale, this is a remote CPU-exhaustion resource-exhaustion vector that degrades or denies handshake service for legitimate peers on the target node, since the expensive step precedes any authentication check.

### Likelihood Explanation
The path is reachable directly from the public UDP listener with no prerequisites — no valid certificate, no prior handshake, and no rate limiting stand in the way. The `remote_allow_list` check in `HandleIncoming` only filters by source underlay IP when configured, which does not stop an attacker with an allowed (or unrestricted) network position from sending the crafted packets. The explicit admission in `handshake.proto` that the intended anti-DoS cookie was never implemented is direct evidence the maintainers recognized this gap without closing it. [5](#0-4) 

### Recommendation
Introduce a cost/verification step before performing the Noise `ReadMessage` DH computation for unsolicited stage-1 packets — e.g., implement the previously-reserved `Cookie` anti-DoS mechanism (stateless cookie/proof-of-work challenge-response) so a responder does not commit CPU cycles until the initiator demonstrates a minimal cost or return-routability, and add a per-source-IP rate limit on `beginHandshake` invocations in `HandshakeManager.HandleIncoming`.

### Proof of Concept
1. Craft a UDP packet with a valid Nebula header (`header.Handshake`, `Subtype = header.HandshakeIXPSK0`, `RemoteIndex = 0`, `MessageCounter = 1`) followed by an arbitrary (or replayed) Noise IX first-message body — no valid CA-signed certificate is required to reach the costly path.
2. Send this packet repeatedly, varying nothing but the source port/from arbitrary source addresses, to a target node's UDP listener.
3. Observe that each packet causes the target to allocate a `handshake.Machine`, execute `ProcessPacket` → Noise `ReadMessage` (DH computation) in `handshake_manager.go`'s `beginHandshake`, before certificate validation ever runs — confirmed by the code path in [6](#0-5)  and [7](#0-6) , with no rate limiter or cookie check anywhere in front of it.

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

**File:** handshake/machine.go (L203-241)
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

	// From here on, noise state has advanced. Any error is fatal.
	flags := m.peerMsgFlags()

	if err := m.processPayload(msg, flags); err != nil {
		return nil, nil, err
	}
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
