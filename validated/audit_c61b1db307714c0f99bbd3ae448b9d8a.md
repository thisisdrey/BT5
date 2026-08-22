### Title
Unauthenticated attackers can flood a node with handshake-initiation packets to force expensive, uncosted Noise crypto work with no rate limiting, causing DOS - (File: handshake_manager.go)

### Summary
The reported bug class is "unauthenticated/no-cost repeated calls to an expensive state-changing operation with no minimum-cost check, enabling DOS/resource drain" (SUI `gateway.move::deposit`, allowing zero-value deposits with no fee/rate check). The reachable Nebula analog is `HandshakeManager.HandleIncoming` / `beginHandshake` in `handshake_manager.go`, which processes every incoming stage-1 handshake packet from an arbitrary, uncertified remote UDP address by immediately running full Noise `ReadMessage` (X25519 DH + crypto) before any certificate is verified, with no per-source rate limiting, cost, or proof-of-work gate.

### Finding Description
Any UDP peer — with no CA-signed certificate — can send a well-formed stage-1 handshake packet (`h.MessageCounter == 1`, `h.RemoteIndex == 0`) to a Nebula node. `HandleIncoming` performs only cheap header checks (subtype match, `RemoteIndex==0`, and an IP allow-list check) before calling `beginHandshake`: [1](#0-0) 

`beginHandshake` then unconditionally constructs a new `handshake.Machine` and calls `machine.ProcessPacket`, which performs the actual Noise `ReadMessage` (asymmetric crypto / DH) *before* any certificate validation occurs — certificate verification (`m.verifier`, backed by `CAPool.VerifyCertificate`) only happens after the expensive crypto step, inside `processPayload`/`validateCert`: [2](#0-1) [3](#0-2) [4](#0-3) 

Critically, the wire protocol explicitly documents that the anti-DoS mechanism (`Cookie` field) was reserved but "never implemented" and "no released version of nebula has ever populated it": [5](#0-4) 

There is no per-source-IP rate limiter, proof-of-work, or handshake-cost gate anywhere in the incoming handshake path (`HandleIncoming` → `beginHandshake`) — the only gate is `GetRemoteAllowList().AllowUnknownVpnAddr`, which is a static, admin-configured IP allow/deny list (default allow-all), not a dynamic rate limiter: [6](#0-5) 

This mirrors the SUI report's root cause exactly: an entry point reachable by any unauthenticated party, invoked as many times as desired, that triggers non-trivial, resource-consuming work with no minimum-cost/throttle check to price out abuse.

### Impact Explanation
Each spoofed stage-1 packet forces the responder to perform a full asymmetric Noise handshake step (X25519 DH computation) and allocate a new `handshake.Machine`/`HandshakeHostInfo` object before the certificate — and therefore the sender's legitimacy — is ever checked. An attacker (or botnet) can send a stream of forged, distinct-source stage-1 packets to force repeated expensive crypto operations and memory allocation on the target node, exhausting CPU and/or memory and degrading or denying service to legitimate peers trying to complete real handshakes. This is a remote, unauthenticated-reachable resource-exhaustion/DOS vector against the node itself.

### Likelihood Explanation
High likelihood: crafting a stage-1 handshake packet requires no certificate or prior trust relationship — only knowledge of the target's UDP listening port and the fixed header format (`header.HandshakeIXPSK0`, `MessageCounter=1`, `RemoteIndex=0`). The default remote allow list permits unknown VPN addresses unless explicitly configured otherwise. The absence of any rate limiting or cost gate for this path is confirmed directly by the protocol comment marking the intended Cookie-based anti-DoS mechanism as never implemented.

### Recommendation
Add a proof-of-work/cookie mechanism (the reserved `Cookie` field) or a per-source-address rate limiter/token bucket in `HandshakeManager.HandleIncoming` before `beginHandshake` invokes `machine.ProcessPacket`, so an unauthenticated sender cannot force unbounded Noise crypto operations. Consider deferring/limiting the number of concurrent pending responder `Machine`s per source IP, and reject/throttle repeated stage-1 packets from the same address within a short window prior to running any cryptographic operation.

### Proof of Concept
1. Craft a UDP packet with `header.Encode(data, header.Version, header.Handshake, header.HandshakeIXPSK0, 0 /*RemoteIndex*/, 1 /*MessageCounter*/)` followed by arbitrary/garbage bytes as the Noise payload (this mirrors the packet construction already used by `makeHandshakePacket` in the test suite): [7](#0-6) 
2. Send this packet (with a spoofed/varying source `UdpAddr`) repeatedly and rapidly to a target Nebula node's UDP listener.
3. Each packet reaches `HandleIncoming` → `beginHandshake` → `machine.ProcessPacket` → `hs.ReadMessage`, performing an X25519 DH operation and allocating a new `Machine` and `HostInfo`-adjacent state per packet, all before certificate verification fails and the packet is discarded.
4. Repeating this at high volume from many source addresses (or via amplification) forces the node to spend CPU/crypto and memory on every packet, unauthenticated and uncosted, producing the DOS condition analogous to the SUI zero-value-deposit spam.

### Citations

**File:** handshake_manager.go (L151-184)
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

**File:** handshake/machine.go (L223-241)
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
