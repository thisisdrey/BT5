### Title
Unauthenticated, cost-free responder-side crypto work per handshake packet enables cheap DoS / spoofed-source amplification - (File: handshake_manager.go)

### Summary
Every UDP packet whose header claims to be a stage-1 handshake (`MessageCounter == 1`, `RemoteIndex == 0`) causes the responder to allocate a fresh Noise `Machine` and run the full IX cryptographic exchange (`ProcessPacket` → Noise `ReadMessage`, an X25519 ECDH + AEAD decrypt) *before* any certificate, CA, or identity check occurs. There is no rate limit, proof-of-work, or stateless-cookie challenge gating this work, and the wire format's own documentation records that the anti-DoS mechanism ("Cookie") intended for this purpose was designed but "never implemented." This mirrors the Well/AMM finding: the defender bears real, non-trivial cost (cryptographic computation, and potentially a full stage-2 reply sent to an attacker-chosen/spoofed UDP source) while the attacker's cost is a single crafted packet, making a sustained denial-of-service or reflective amplification attack economically trivial for anyone with no CA-signed certificate.

### Finding Description
`HandshakeManager.HandleIncoming` only performs cheap, header-level filtering (subtype allow-list, IP allow-list, `RemoteIndex == 0` check) before handing any first-stage packet to `beginHandshake`: [1](#0-0) 

`beginHandshake` then unconditionally constructs a new `handshake.Machine` and calls `machine.ProcessPacket`, which performs the Noise `ReadMessage` (ECDH + AEAD) step, for any packet that reaches this point — no proof of legitimate ownership of a CA-signed certificate is required or possible to check yet, because cert validation happens only after the cryptographic exchange completes: [2](#0-1) 

Certificate/identity verification (`validatePeerCert`) only runs *after* `machine.ProcessPacket` has already spent the CPU cost of the Diffie-Hellman computation and, in the IX pattern, has already produced/queued a stage-2 response: [3](#0-2) 

The protocol's own wire-format documentation explicitly acknowledges that a mitigating anti-DoS mechanism was designed but never shipped: [4](#0-3) 

Because Nebula runs over UDP, the source address on `via.UdpAddr` used to send the stage-2 response is attacker-controlled/spoofable at the network layer; `sendHandshakeResponse` writes the (expensive-to-produce) reply straight to that address: [5](#0-4) 

This is directly analogous to the reported Well finding: in both cases, the system performs its "expensive" state-changing operation (constant-product recomputation in the Well; ECDH+AEAD handshake crypto in Nebula) at effectively zero cost to the attacker and with no fee/challenge mechanism to make repeated abuse costly, while the victim absorbs the real cost.

### Impact Explanation
An attacker with no CA-signed certificate can force the responder to perform real asymmetric cryptography for every spoofed or replayed stage-1 packet sent to it. At sufficient volume this exhausts CPU on the target node, degrading or denying legitimate handshakes/service (DoS) — the "remote crash/DoS impact" class explicitly permitted by scope. Because the stage-2 reply is addressed to the (spoofable) UDP source of the inbound packet, the same primitive can be abused to direct handshake-reply traffic at a third party (reflection), compounding the disruption beyond the responder itself. As with the Well report, the attack requires no privileged access or certificate — only crafted UDP packets — and its cost asymmetry (near-zero attacker cost vs. real defender cost) is exactly the condition the original report and its judges found to be a legitimate, economically viable DoS vector.

### Likelihood Explanation
Likelihood is high: the responder path is reachable by any UDP sender able to route packets to the node's listen port, requires no cryptographic secret or CA trust to trigger the expensive step, and the codebase itself documents (via the deprecated `Cookie` field comment) that the intended mitigation was never built. No additional preconditions (valid certificate, established tunnel, or prior trust relationship) are needed to reach `beginHandshake`.

### Recommendation
Add a lightweight, stateless anti-DoS gate before performing the expensive Noise exchange — e.g., implement the originally-reserved cookie/stateless-retry challenge so the responder only commits to full ECDH/AEAD work after the initiator has demonstrated return-routability (matching a server-issued token), and/or add per-source-IP rate limiting on stage-1 handshake processing in `HandshakeManager.HandleIncoming`/`beginHandshake`. This restores the cost asymmetry in the defender's favor, similar to how imposing a fee mitigates the analogous Well DoS.

### Proof of Concept
1. Craft a UDP packet whose header sets `Type = Handshake`, `Subtype = HandshakeIXPSK0`, `MessageCounter = 1`, `RemoteIndex = 0`, followed by a syntactically valid (but not necessarily CA-signed) Noise IX message-1 payload containing an arbitrary/precomputed ephemeral public key and ciphertext.
2. Send this packet (optionally with a spoofed source `UdpAddr`) to the target Nebula node's listen port.
3. `HandleIncoming` passes the header checks and calls `beginHandshake`, which builds a `Machine` and calls `ProcessPacket`, forcing the responder to perform an X25519 ECDH computation and AEAD decrypt attempt — real CPU cost — before any certificate/CA validation occurs.
4. Repeat step 1–3 at volume from spoofed sources; because there is no cookie/rate limit (as noted in `handshake.proto`'s `Cookie` comment), each packet costs the attacker only bandwidth/crafting time while costing the responder real cryptographic work and (when the exchange is well-formed enough to complete) a stage-2 reply sent to the attacker-chosen address.

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

**File:** handshake_manager.go (L698-726)
```go
// beginHandshake handles an incoming handshake packet that doesn't match any
// existing pending handshake. It creates a new responder Machine and processes
// the first message.
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

**File:** handshake_manager.go (L740-789)
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

	msg := "Handshake message received"
	if !anyVpnAddrsInCommon {
		msg = "Handshake message received, but no vpnNetworks in common."
	}
	f.l.Info(msg,
		"vpnAddrs", vpnAddrs,
		"from", via,
		"certName", remoteCert.Certificate.Name(),
		"certVersion", remoteCert.Certificate.Version(),
		"fingerprint", remoteCert.Fingerprint,
		"issuer", remoteCert.Certificate.Issuer(),
		"initiatorIndex", result.RemoteIndex,
		"responderIndex", result.LocalIndex,
		"handshake", m{"stage": uint64(machine.MessageIndex()), "style": header.SubTypeName(header.Handshake, machine.Subtype())},
	)

	// packet aliases the listener's incoming buffer, so this copy must stay.
	hostinfo.HandshakePacket[handshakePacketStage0] = make([]byte, len(packet[header.Len:]))
	copy(hostinfo.HandshakePacket[handshakePacketStage0], packet[header.Len:])

	// response was freshly allocated by ProcessPacket; safe to retain directly.
	if response != nil {
		hostinfo.HandshakePacket[handshakePacketStage2] = response
	}
```

**File:** handshake_manager.go (L1045-1080)
```go
func (hm *HandshakeManager) sendHandshakeResponse(via ViaSender, msg []byte, hostinfo *HostInfo, cached bool) {
	if msg == nil {
		return
	}

	f := hm.f
	f.messageMetrics.Tx(header.Handshake, header.MessageSubType(msg[1]), 1)

	// Common log fields. peerCert may be nil during intermediate
	// multi-message flows (handshake hasn't completed yet); skip the cert
	// block if so.
	logFields := []any{
		"vpnAddrs", hostinfo.vpnAddrs,
		"handshake", m{"stage": uint64(2), "style": header.SubTypeName(header.Handshake, header.HandshakeIXPSK0)},
		"cached", cached,
		"initiatorIndex", hostinfo.remoteIndexId,
		"responderIndex", hostinfo.localIndexId,
	}
	if peerCert := hostinfo.ConnectionState.peerCert; peerCert != nil {
		logFields = append(logFields,
			"certName", peerCert.Certificate.Name(),
			"certVersion", peerCert.Certificate.Version(),
			"fingerprint", peerCert.Fingerprint,
			"issuer", peerCert.Certificate.Issuer(),
		)
	}

	if !via.IsRelayed {
		fields := append(logFields, "from", via)
		err := f.outside.WriteTo(msg, via.UdpAddr)
		if err != nil {
			f.l.Error("Failed to send handshake message", append(fields, "error", err)...)
		} else {
			f.l.Info("Handshake message sent", fields...)
		}
	} else {
```

**File:** handshake/handshake.proto (L17-25)
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
```
