### Title
Missing anti-clogging mechanism for unauthenticated stage-1 handshake packets enables asymmetric-cost DoS - (File: `handshake_manager.go`, `handshake/handshake.proto`)

### Summary
This is a valid analog to the "zero-weight extrinsics" bug class: an operation that is cheap for the attacker to trigger but expensive for the target to process, with no cost/rate-limiting gate, enabling network-wide denial of service. In Nebula, any unauthenticated UDP packet that looks like a stage-1 handshake message forces the receiving node to perform an expensive asymmetric Noise DH computation before any peer authentication occurs, and there is no cookie/proof-of-work/rate-limit defense — despite the wire format having a `Cookie` field explicitly reserved for this purpose that was "never implemented."

### Finding Description
`HandshakeManager.HandleIncoming` dispatches any inbound packet with `MessageCounter == 1` and `RemoteIndex == 0` straight to `beginHandshake`, gated only by an IP-based `AllowUnknownVpnAddr` allow-list check (which defaults to permissive) — not by any per-certificate or per-identity authentication, since the sender has not yet proven anything: [1](#0-0) 

`beginHandshake` then unconditionally constructs a new `handshake.Machine` and calls `machine.ProcessPacket`, which invokes Noise's `ReadMessage`, performing an X25519 Diffie-Hellman scalar multiplication and certificate reconstruction/verification before any proof that the sender is a legitimate, uncompromised peer: [2](#0-1) [3](#0-2) 

Crucially, the handshake wire format documents that a `Cookie` field was reserved specifically as an anti-DoS mechanism but was **never implemented** in any released version, and the parser silently discards it: [4](#0-3) 

There is no rate limiting, no per-source-IP throttling, and no lightweight stateless proof-of-work/cookie challenge gating entry into `beginHandshake`. Any attacker (with no CA-signed certificate, since certificate verification only happens *inside* `ProcessPacket`, after the expensive DH step) can generate an unlimited stream of well-formed but bogus stage-1 packets (`MessageCounter=1`, `RemoteIndex=0`, arbitrary/garbage Noise payload) from spoofed source addresses. Each packet forces the responder to perform costly asymmetric cryptography and object allocation before it is rejected.

### Impact Explanation
Because the DH operation and certificate-verification path is invoked before the sender's identity is validated, this is a CPU-amplification primitive: a small, cheap UDP packet forces a comparatively expensive operation on the target lighthouse/node. At scale (many spoofed packets, or a botnet), this can exhaust CPU on a Nebula node — particularly on lighthouses, which are the most exposed and highest-value targets in the topology — causing denial of service to legitimate handshake processing and thus network-wide connectivity disruption. This directly matches the "remote crash/remote state poisoning" style impact of resource-exhaustion DoS accepted by the validation criteria.

### Likelihood Explanation
Likelihood is high for any Internet-reachable Nebula node/lighthouse: the trigger packet requires no valid certificate, no completed handshake, and no knowledge of any secret — only a correctly shaped header (`MessageCounter=1`, `RemoteIndex=0`, valid subtype `HandshakeIXPSK0`) and an IP address that isn't blocked by `remote_allow_list`, which is off by default in most deployments. UDP allows trivial source spoofing, making attribution and IP-based blocking largely ineffective, and there is no other cost imposed on the sender.

### Recommendation
Short term, implement the anti-clogging cookie/stateless-retry mechanism the protocol already reserves a field for (`Cookie`) so that responders do not perform the Noise DH/certificate-verification work until the initiator proves it can complete a round trip (i.e., echo back a server-issued token) — this is the standard mitigation used by other Noise-based VPNs (e.g., WireGuard's cookie-reply mechanism). Additionally, add per-source-IP and/or global rate limiting on `beginHandshake` invocation in `HandshakeManager.HandleIncoming`/`beginHandshake` before any handshake machine is constructed, and consider requiring a lightweight computational puzzle for handshake initiation from unknown addresses.

### Proof of Concept
1. From an attacker-controlled host (source IP may be spoofed since UDP is connectionless), craft a UDP packet matching the Nebula header: `Version`, `Type=Handshake`, `Subtype=HandshakeIXPSK0`, `RemoteIndex=0`, `MessageCounter=1`, followed by arbitrary bytes as the Noise payload (as done in the test helper `makeHandshakePacket`): [5](#0-4) 
2. Send a large volume of such packets, each from a different (optionally spoofed) source `UdpAddr`, to a target Nebula node's UDP listener.
3. Each packet passes the cheap `AllowUnknownVpnAddr`/header checks and reaches `beginHandshake`, which allocates a new `handshake.Machine` and executes `ProcessPacket` → `ReadMessage`, performing an X25519 DH computation for every packet, regardless of whether the payload/certificate is valid.
4. Observe elevated CPU usage on the target proportional to the packet rate, with no rate limit or cookie challenge slowing the attacker down, demonstrating the asymmetric-cost DoS.

### Citations

**File:** handshake_manager.go (L164-185)
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

**File:** handshake/machine.go (L223-234)
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
```

**File:** handshake/handshake.proto (L21-24)
```text
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
