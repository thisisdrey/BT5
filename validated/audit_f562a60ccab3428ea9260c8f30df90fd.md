Confirmed: `AllowUnknownVpnAddr` defaults to `true` when no `remote_allow_list` is configured (`allow_list.go:270-275`), meaning by default any source address may trigger `beginHandshake`. Each such packet forces the responder to run `handshake.NewMachine` and `Machine.ProcessPacket`, which performs an expensive Noise `ReadMessage` (asymmetric X25519 Diffie-Hellman operation) and certificate reconstruction/verification, with no cost imposed on the sender and no deduplication/rate-limit prior to that expensive work. The protocol's own `Cookie` field was explicitly reserved for anti-DoS purposes but "was never implemented" (`handshake/handshake.proto:21-23`), confirming there is no cookie/proof-of-work/rate-limit gate before the expensive path runs.

### Title
Unmetered, unauthenticated stage-1 handshake packets allow computational-cost DoS flood on responders - (File: handshake_manager.go)

### Summary
Any UDP peer (no valid CA-signed certificate required, since stage-1 has no prior authentication) can flood a Nebula node with spoofed/garbage stage-1 handshake packets. Each packet forces the responder to construct a fresh Noise `Machine` and execute an expensive asymmetric Diffie-Hellman operation via `ProcessPacket`, before any certificate is validated and before any per-source cost or rate limiting is applied. This mirrors the SEDA "free gas" flood: unmetered, attacker-controlled work is performed on every received message with no cost/dedup gate.

### Finding Description
`Interface.readOutsidePackets` routes any packet with `h.Type == header.Handshake` directly to `f.handshakeManager.HandleIncoming` without requiring a prior authenticated session [1](#0-0) .

`HandshakeManager.HandleIncoming` gates only on cheap header checks: known subtype, and `RemoteIndex == 0` for `MessageCounter == 1`. If those pass, and (by default, since `remote_allow_list` is unset and `AllowUnknownVpnAddr` returns `true` for a nil `RemoteAllowList`) the source is not blocked, it unconditionally calls `beginHandshake` [2](#0-1) [3](#0-2) .

`beginHandshake` builds a brand-new Noise handshake `Machine` and immediately calls `machine.ProcessPacket`, which invokes `m.hs.ReadMessage`, performing a real Noise/X25519 Diffie-Hellman computation, before any certificate has been validated [4](#0-3) [5](#0-4) .

Crucially, there is no cost imposed on the sender and no anti-flood mechanism at this stage: the wire format's `Cookie` field was reserved specifically for an anti-DoS scheme but documentation explicitly states it "was never implemented" and is silently skipped by the parser [6](#0-5) . Unlike stage-2+ continuation packets, which are gated behind `queryIndex` lookup (cheap, O(1) map lookup, dropped if no match) [7](#0-6) , stage-1 packets always pay the full DH cost regardless of legitimacy, and an attacker can trivially generate unlimited distinct stage-1 packets (varying content bytes) so no packet-level deduplication (`ErrAlreadySeen`, which only fires later inside `CheckAndComplete` after the DH work is already done) prevents the flood [8](#0-7) .

### Impact Explanation
An attacker with no valid certificate and no established tunnel can force a victim node to perform expensive asymmetric cryptographic operations for every spoofed stage-1 packet sent, at line rate, with zero cost to the attacker (no reply required, no PoW, no cookie). This is a remote CPU-exhaustion / node DoS vector, directly analogous to the SEDA "unmetered execution message flood" bug class: unauthenticated, uncosted messages that trigger expensive backend work can delay or halt normal operation of the node (dropped handshakes for legitimate peers, mesh instability).

### Likelihood Explanation
High. No credentials, certificates, or prior handshake state are required. The only precondition is default configuration (no `lighthouse.remote_allow_list` restricting unknown VPN addresses), which is the common/default deployment posture. Attack packets can be trivially generated at high volume since the header only requires a valid subtype and `RemoteIndex == 0`; payload content is not validated before the DH step.

### Recommendation
Implement the reserved-but-unimplemented anti-DoS cookie/challenge mechanism (or equivalent rate limiting/proof-of-work) before performing `ProcessPacket`/`ReadMessage` on stage-1 packets, so that responding to an unauthenticated stage-1 message costs the sender at least as much as it costs the responder. Alternatively, add per-source-address rate limiting on stage-1 handshake initiation in `HandleIncoming` before calling `beginHandshake`, so a flood of spoofed/distinct stage-1 packets cannot force unlimited free Noise DH computations.

### Proof of Concept
1. Attacker crafts arbitrary UDP packets with `header.Handshake` type, `header.HandshakeIXPSK0` subtype, `RemoteIndex = 0`, and `MessageCounter = 1`, with random/garbage payload bytes (analogous to `makeHandshakePacket` used in tests) [9](#0-8) .
2. Attacker sends a high volume of such packets, each with distinct bogus payload content, from spoofed or ephemeral source addresses to the victim's listen UDP port.
3. Each packet reaches `HandleIncoming` → `beginHandshake` → `Machine.ProcessPacket` → `hs.ReadMessage`, performing a full Noise DH operation and (if payload happens to parse) certificate reconstruction, before being rejected — with no per-source cost check or cookie challenge in between.
4. Repeating step 2 at sufficient volume consumes victim CPU cycles on genuine cryptographic operations, degrading or halting legitimate handshake processing on the node.

### Citations

**File:** outside.go (L76-79)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return
```

**File:** handshake_manager.go (L172-184)
```go
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
```

**File:** handshake_manager.go (L187-193)
```go
	// Continuation message must match a pending handshake by index.
	// Anything else is an orphaned packet (e.g., late retransmit after
	// timeout) and is dropped.
	if hh := hm.queryIndex(h.RemoteIndex); hh != nil {
		hm.continueHandshake(via, hh, packet)
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

**File:** handshake_manager.go (L797-801)
```go
	existing, err := hm.CheckAndComplete(hostinfo, handshakePacketStage0, f)
	if err != nil {
		hm.handleCheckAndCompleteError(err, existing, hostinfo, via)
		return
	}
```

**File:** allow_list.go (L270-275)
```go
func (al *RemoteAllowList) AllowUnknownVpnAddr(vpnAddr netip.Addr) bool {
	if al == nil {
		return true
	}
	return al.AllowList.Allow(vpnAddr)
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

**File:** handshake/handshake.proto (L20-24)
```text
  uint32 ResponderIndex = 3;
  // Cookie was reserved for an anti-DoS mechanism that was never
  // implemented. No released version of nebula has ever populated it; the
  // hand-written parser silently skips it on read.
  uint64 Cookie = 4 [deprecated = true];
```

**File:** e2e/handshake_manager_test.go (L20-28)
```go
// makeHandshakePacket creates a handshake packet with the given parameters.
func makeHandshakePacket(from, to netip.AddrPort, subtype header.MessageSubType, remoteIndex uint32, counter uint64) *udp.Packet {
	data := make([]byte, 200)
	header.Encode(data, header.Version, header.Handshake, subtype, remoteIndex, counter)
	for i := header.Len; i < len(data); i++ {
		data[i] = byte(i)
	}
	return &udp.Packet{To: to, From: from, Data: data}
}
```
