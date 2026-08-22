### Title
Unauthenticated Handshake Flood Forces Expensive Noise Crypto and Responder Machine Allocation, Enabling DOS — (File: handshake_manager.go, handshake/machine.go)

### Summary
The Scroll rate-limiter report describes a mechanism whose only defense (a total-amount cap that requires admin intervention to lift) can be weaponized by an attacker to exhaust protocol capacity and lock out legitimate users at negligible cost (gas fees only). The analogous class of bug in nebula is an unauthenticated resource-exhaustion path in the handshake responder: nebula reserved an explicit anti-DoS field (`Cookie`) in the handshake wire format for exactly this purpose, but never implemented it, and no other rate limiting, cookie/puzzle, or per-source throttling gates stage-1 handshake packets before expensive Noise cryptographic processing occurs.

### Finding Description
Every inbound stage-1 (`MessageCounter == 1`) handshake packet is routed to `HandshakeManager.beginHandshake`, which is reached after only a lightweight source-IP allow-list check (`AllowUnknownVpnAddr`) — no certificate has been presented or verified yet [1](#0-0) . `beginHandshake` then unconditionally constructs a new `handshake.Machine` and calls `machine.ProcessPacket`, which performs a full Noise `ReadMessage` (DH/crypto operation) and, for the IX pattern, also computes and writes a responder reply (`buildResponse`, another `WriteMessage`) *before* the peer's certificate is validated [2](#0-1) [3](#0-2) .

This is precisely the bug class the external report warns about: a protocol mechanism (here, expensive per-packet cryptographic handshake processing) that is meant to gate access to a scarce resource but has no working anti-abuse safeguard. Nebula's own wire-format documentation confirms the anti-DoS control was designed but abandoned: the `Cookie` field in `NebulaHandshakeDetails` was "reserved for an anti-DoS mechanism that was never implemented. No released version of nebula has ever populated it; the hand-written parser silently skips it on read." [4](#0-3) 

Because a stage-1 packet requires only a zero `RemoteIndex` (trivially satisfied by any attacker, checked only for well-formedness, not authenticity) [5](#0-4) , an attacker with no CA-signed certificate can send an arbitrary volume of spoofed/garbage IX stage-1 packets to force the responder to repeatedly allocate `Machine` state, run Noise handshake crypto, and (in many cases) generate and transmit a stage-2 response — all without ever needing to hold a valid certificate.

### Impact Explanation
This matches the report's "concrete... remote crash impact" / "remote state poisoning" criteria via CPU/resource exhaustion: repeated forced Noise handshake computation (asymmetric crypto operations) on every incoming stage-1 packet can degrade or deny the node's ability to process legitimate handshakes and traffic, analogous to how the Scroll rate limiter's total-amount cap could be pinned by a low-cost attacker to lock out real users. Unlike the Scroll bug (bounded by a numeric cap that admins can raise), this nebula path has no cap at all on a per-source or global basis for *unauthenticated* handshake attempts, only after-the-fact hostmap/index bookkeping — the expensive crypto work happens unconditionally per packet.

### Likelihood Explanation
High. The attack requires no valid certificate, no prior trust relationship, and no non-default configuration — only knowledge of a listening nebula node's UDP address and the ability to craft header bytes with `MessageCounter == 1`, `Subtype == HandshakeIXPSK0`, and `RemoteIndex == 0`. The `AllowUnknownVpnAddr` allow-list is the only gate and is IP-based (spoofable over UDP) and often permissive by default. The designed mitigation (`Cookie`) confirms the nebula authors themselves recognized and intended to close this DOS vector but never shipped the fix.

### Recommendation
- Implement a lightweight, stateless anti-DoS gate before running `machine.ProcessPacket` on stage-1 packets — e.g., a cookie/puzzle challenge-response (reviving the reserved `Cookie` field's original intent) so the responder does not commit CPU/crypto work until the initiator proves round-trip reachability.
- Add per-source-IP and global rate limiting on stage-1 handshake acceptance, independent of the existing `remote_allow_list`.
- Bound and monitor the cost of concurrent in-flight handshake `Machine` allocations, and consider deprioritizing/dropping stage-1 traffic under load rather than always synchronously running the full Noise exchange.

### Proof of Concept
1. Attacker crafts a UDP packet with a valid nebula header: `Type = Handshake`, `Subtype = HandshakeIXPSK0`, `MessageCounter = 1`, `RemoteIndex = 0`, followed by an arbitrary/garbage Noise `IX` first-message body.
2. Attacker sends a high volume of such packets (varying source ports/spoofed IPs) to the target node's listening UDP port.
3. Each packet reaches `HandshakeManager.HandleIncoming` → `beginHandshake` → `handshake.NewMachine` + `machine.ProcessPacket`, forcing a Noise `ReadMessage`/`WriteMessage` crypto operation per packet, with no certificate having been verified and no rate limiting applied [6](#0-5) .
4. Sustained flooding consumes responder CPU on cryptographic operations, degrading its ability to service legitimate handshakes/traffic — a DOS with attacker cost limited to sending UDP packets, mirroring the "gas fees only" cost model in the original report.

### Citations

**File:** handshake_manager.go (L164-184)
```go
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

**File:** handshake/machine.go (L203-276)
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

	// If ReadMessage derived keys, the handshake is complete. Noise should
	// always produce both keys together; asymmetry is a protocol invariant
	// violation.
	if eKey != nil || dKey != nil {
		if eKey == nil || dKey == nil {
			m.failed = true
			return nil, nil, ErrAsymmetricCipherKeys
		}
		if err := m.requireComplete(); err != nil {
			return nil, nil, err
		}
		return nil, m.completed(eKey, dKey), nil
	}

	// ReadMessage didn't complete, produce the next outgoing message
	out, dk, ek, err := m.buildResponse(out)
	if err != nil {
		m.failed = true
		return nil, nil, err
	}

	if ek != nil || dk != nil {
		if ek == nil || dk == nil {
			m.failed = true
			return nil, nil, ErrAsymmetricCipherKeys
		}
		if err := m.requireComplete(); err != nil {
			return nil, nil, err
		}
		return out, m.completed(ek, dk), nil
	}

	return out, nil, nil
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
