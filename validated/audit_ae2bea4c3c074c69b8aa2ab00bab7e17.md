### Title
Unauthenticated Repeated Handshake-Initiation Flood Causes Unbounded Noise/Cert-Verification Work (Griefing/CPU-Exhaustion DoS) - ([File: handshake_manager.go])

### Summary
Any remote UDP sender — with no valid certificate and no prior state — can repeatedly send stage-1 handshake packets (`MessageCounter == 1`, `RemoteIndex == 0`) to a Nebula node. Each such packet causes `HandshakeManager.HandleIncoming` to invoke `beginHandshake`, which unconditionally constructs a new Noise handshake `Machine` and runs `machine.ProcessPacket`, performing a full Diffie-Hellman computation and (for a well-formed packet) certificate reconstruction/verification — before any authentication of the sender succeeds. There is no per-source rate limiting, proof-of-work, or stateless cookie check gating this expensive work.

### Finding Description
`HandleIncoming` dispatches any packet with `MessageCounter == 1` and `RemoteIndex == 0` straight to `beginHandshake`: [1](#0-0) 

`beginHandshake` then always builds a fresh handshake `Machine` and calls `ProcessPacket`, which performs the Noise `ReadMessage` (an ECDH operation) unconditionally, regardless of whether the packet ultimately yields a valid certificate: [2](#0-1) [3](#0-2) 

Only after this expensive cryptographic step is the peer certificate checked, and only then is the sender's identity validated via `validatePeerCert`: [4](#0-3) 

There is no fee, cost, or rate-limit mechanism gating this path per source address. The `remote_allow_list` check in `HandleIncoming` only filters based on statically configured CIDR ranges, not on request volume, and does not prevent an already-allowed (or unauthenticated but permitted) peer from flooding stage-1 packets. Notably, the wire protocol itself documents that an anti-DoS mechanism (`Cookie`) was designed but never implemented: [5](#0-4) 

This directly mirrors the reported bug class: a function reachable by an unauthenticated caller performs an expensive, stateful operation (VRF request in the original report; Noise DH + cert verification here) repeatedly at no cost to the caller, enabling a griefing/resource-exhaustion attack.

### Impact Explanation
An attacker with no certificate and no established tunnel can send a stream of spoofed or replayed stage-1 handshake packets to any Nebula node's UDP listener. Each packet forces the target to perform elliptic-curve Diffie-Hellman computation (and, for well-formed packets, certificate parsing/signature verification) synchronously in the packet-processing path. Sustained flooding from one or many source addresses can exhaust CPU on the target node, degrading or denying service to legitimate tunnel peers — a remote, unauthenticated DoS/griefing condition.

### Likelihood Explanation
High. The attack requires only the ability to send UDP packets to the target's listening port with a trivially constructed header (`Type=Handshake`, `Subtype=HandshakeIXPSK0`, `MessageCounter=1`, `RemoteIndex=0`); no certificate, prior handshake state, or successful authentication is required to reach the expensive `ProcessPacket`/`ReadMessage` code path. No rate limiting or cost mechanism currently exists to prevent this.

### Recommendation
Introduce a lightweight, stateless anti-DoS check before performing the Noise DH/cert-verification work in `beginHandshake` — e.g., implement the previously-reserved cookie/stateless-retry mechanism referenced in `handshake.proto`, or add per-source-IP rate limiting for stage-1 handshake initiations in `HandleIncoming` before dispatching to `beginHandshake`. This ensures expensive cryptographic work is only performed after a cheap validity/liveness check on the source.

### Proof of Concept
1. Attacker crafts a UDP packet with a Nebula header: `Type=Handshake`, `Subtype=HandshakeIXPSK0`, `MessageCounter=1`, `RemoteIndex=0`, and an arbitrary/garbage Noise payload.
2. Attacker sends this packet repeatedly (potentially from many source ports/spoofed addresses) to the victim's UDP listener.
3. Each packet reaches `HandleIncoming` → `beginHandshake` → `machine.ProcessPacket` → `m.hs.ReadMessage`, performing a DH operation per packet with no cost or rate limit to the attacker.
4. Repeating this at high volume consumes victim CPU cycles disproportionately to the attacker's cost, degrading service for legitimate peers.

### Citations

**File:** handshake_manager.go (L172-185)
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

**File:** handshake_manager.go (L740-750)
```go
	remoteCert := result.RemoteCert
	if remoteCert == nil {
		f.l.Error("Handshake did not produce a peer certificate", "from", via)
		return
	}

	// Validate peer identity
	vpnAddrs, anyVpnAddrsInCommon, ok := hm.validatePeerCert(via, remoteCert)
	if !ok {
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
