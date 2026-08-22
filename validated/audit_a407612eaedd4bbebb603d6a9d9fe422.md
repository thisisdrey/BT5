### Title
Unauthenticated Stage-1 Handshake Packets Trigger Unbounded, CPU-Expensive Noise/Cert Processing With No Rate Limiting - (File: handshake_manager.go)

### Summary
Any UDP peer, without holding a CA-signed certificate or completing authentication, can send an unlimited stream of forged `stage-1` (`HandshakeIXPSK0`, `MessageCounter == 1`, `RemoteIndex == 0`) handshake packets. Each one causes `HandshakeManager.HandleIncoming` to route into `beginHandshake`, which allocates a new Noise `handshake.Machine`, performs a full X25519 Diffie-Hellman `ReadMessage` operation, and allocates a `HostInfo`/`ConnectionState`, before any certificate signature is checked against the CA pool. There is no per-source rate limiting on this path, only an optional static IP allow list, so an attacker can exhaust responder CPU and memory with cheap, spoofable packets, matching the HAL-04 "Lack of Resources and Rate Limiting" bug class.

### Finding Description
`Interface.readOutsidePackets` dispatches any packet with `h.Type == header.Handshake` straight to `f.handshakeManager.HandleIncoming` with no throttling: [1](#0-0) 

`HandshakeManager.HandleIncoming` only gates on subtype and a coarse, optional remote allow list before dispatching every stage-1 packet to `beginHandshake`: [2](#0-1) 

`beginHandshake` then unconditionally builds a fresh Noise state machine and calls `machine.ProcessPacket`, which performs the expensive `hs.ReadMessage` (an X25519 ECDH operation) before the peer certificate is unmarshalled, recombined, or verified against the CA pool: [3](#0-2) [4](#0-3) 

Certificate/CA verification (`m.validateCert` → `m.verifier(rc)`) only happens *after* the DH computation has already succeeded, so the costly cryptographic work is spent on packets from a completely unauthenticated, uncertified sender: [5](#0-4) 

Nothing in `HandshakeManager` bounds the number of pending handshakes (`hm.vpnIps`, `hm.indexes`) or rate-limits per-source stage-1 packets — the only check is `hm.lightHouse.GetRemoteAllowList().AllowUnknownVpnAddr(...)`, which is an IP allow/deny list, not a request-rate limiter, and is empty/permissive by default: [6](#0-5) 

Each new attempt also allocates a `HostInfo` with fresh maps for `relayState`: [7](#0-6) 

Because index and vpn addr collisions are only resolved later in `CheckAndComplete`, and a failed/garbage handshake simply falls through without decrementing any counter or being tracked against the source, an attacker can keep the responder performing DH operations and allocating handshake state indefinitely.

### Impact Explanation
An attacker with no valid certificate can flood a nebula node with spoofed UDP stage-1 packets, forcing it to repeatedly perform asymmetric cryptographic (X25519) operations and heap allocations for every packet before any authentication succeeds. Sustained at line rate this exhausts CPU and memory on the responder, degrading or denying service to legitimate peers — a remote, unauthenticated denial-of-service, consistent with the reported "Lack of Resources and Rate Limiting" class (score: Impact 4).

### Likelihood Explanation
Exploitation requires only UDP reachability to the node's listen port; no certificate, prior handshake, or lighthouse trust is needed since `beginHandshake` runs before certificate verification, and by default the `lighthouse.remote_allow_list` is unset/permissive. The report's PoC (300 req/min sustained) demonstrates this class of flood is trivially achievable, and here each packet triggers meaningfully more expensive work (asymmetric crypto + allocation) than a typical API request.

### Recommendation
- Add per-source-IP rate limiting/backoff for inbound stage-1 handshake packets in `HandshakeManager.HandleIncoming` / `beginHandshake`, independent of the existing allow list.
- Cap the number of concurrent pending handshakes (`hm.vpnIps`/`hm.indexes`) and evict/reject new stage-1 attempts once a threshold is reached.
- Consider a lightweight, pre-DH proof-of-work or cookie/anti-spoofing check (as some Noise-based protocols do) before committing to the expensive `ReadMessage` DH step.

### Proof of Concept
1. From an address with no nebula certificate, craft repeated UDP packets shaped as `HandshakeIXPSK0` stage-1 messages (`header.Handshake`, `Subtype = HandshakeIXPSK0`, `MessageCounter = 1`, `RemoteIndex = 0`), with random/garbage Noise payloads.
2. Send these at a high rate to a nebula node's listening UDP port.
3. Observe that each packet reaches `beginHandshake` → `handshake.Machine.ProcessPacket` → `hs.ReadMessage`, performing an X25519 DH computation and allocating a `HostInfo`/`ConnectionState` per packet, with no rate limiting rejecting the flood, consistent with `handshake_manager.go` lines 151-185 and 701-726 and `handshake/machine.go` lines 203-234.

### Citations

**File:** outside.go (L76-79)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return
```

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

**File:** handshake_manager.go (L752-764)
```go
	hostinfo := &HostInfo{
		ConnectionState:   newConnectionStateFromResult(result),
		localIndexId:      result.LocalIndex,
		remoteIndexId:     result.RemoteIndex,
		vpnAddrs:          vpnAddrs,
		HandshakePacket:   make(map[uint8][]byte, 0),
		lastHandshakeTime: result.HandshakeTime,
		relayState: RelayState{
			relays:         nil,
			relayForByAddr: map[netip.Addr]*Relay{},
			relayForByIdx:  map[uint32]*Relay{},
		},
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

**File:** handshake/machine.go (L332-380)
```go
	// Process certificate
	if flags.expectsCert {
		if err := m.validateCert(payload); err != nil {
			return err
		}
	}

	return nil
}

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
