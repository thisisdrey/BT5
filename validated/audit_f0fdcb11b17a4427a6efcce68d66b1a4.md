### Title
Unauthenticated handshake-initiation flood causes unbounded cryptographic-cost denial of service - ([File: handshake_manager.go])

### Summary
`HandshakeManager.HandleIncoming` performs no cost/rate limiting on stage-1 handshake packets before handing them to `beginHandshake`, which allocates a new Noise `handshake.Machine` and runs the full asymmetric-crypto handshake step (`machine.ProcessPacket` → noise `ReadMessage`) for every inbound packet with `MessageCounter == 1` and `RemoteIndex == 0`. Because this happens before any certificate is verified and before any per-source accounting exists, an attacker with no CA-signed certificate can send an unlimited stream of spoofed stage-1 packets and force the responder to spend CPU on expensive cryptographic operations for each one, exhausting the node's processing capacity. This mirrors the reported class of bug: "no restriction on [a cost parameter], so anyone can [drain/exhaust] the [victim's limited resource] and make the system unavailable."

### Finding Description
`readOutsidePackets` routes any packet of `header.Type == header.Handshake` straight to `f.handshakeManager.HandleIncoming(via, packet, h)` with no prior authentication: [1](#0-0) 

`HandleIncoming` checks only cheap header fields (known subtype, zero `RemoteIndex` for stage 1) before calling `beginHandshake`: [2](#0-1) 

`beginHandshake` then unconditionally constructs a new `handshake.Machine` and calls `machine.ProcessPacket`, which performs the Noise `ReadMessage` operation (elliptic-curve crypto) — this cost is paid before the peer certificate has been verified at all: [3](#0-2) 

There is no per-source-IP rate limiter, no proof-of-work/cookie mechanism, and no cap on the number of concurrent pending handshakes an unauthenticated sender can create system-wide; a `grep` for rate-limiting or cookie constructs in the repository returns nothing relevant to handshake initiation. Only `MaxHostInfosPerVpnIp = 5` bounds *established* host-infos per VPN IP after a handshake succeeds, which does not help here because the packets in this attack never reach that stage — the responder pays the crypto cost for every message regardless of whether the handshake later succeeds or fails validation.

### Impact Explanation
An attacker who can send arbitrary UDP packets to a Nebula node's listen port (no valid CA-signed certificate required) can force unbounded consumption of CPU/crypto resources on that node by flooding it with spoofed stage-1 handshake packets. Because each packet is processed synchronously through Noise crypto before any authentication is possible, sustained flooding denies the ability to process legitimate handshakes and traffic — a remote denial-of-service, directly analogous to the reported bug's outcome of "the system becomes unavailable."

### Likelihood Explanation
Low difficulty: no privileged key, certificate, or prior relationship with the target is needed. An attacker only needs to craft a minimal well-formed Nebula header (type=Handshake, valid subtype, `RemoteIndex=0`, `MessageCounter=1`) and send it over UDP, which can be trivially scripted and sent at high volume, optionally from spoofed source addresses since UDP requires no handshake at the transport layer.

### Recommendation
- **Short term:** Add a rate limit (per source `IP`/`AddrPort`, and/or global) on the number of stage-1 handshake packets processed per unit time in `HandshakeManager.HandleIncoming`/`beginHandshake`, and/or a lightweight anti-spoof cost check (e.g., stateless cookie/proof-of-work exchange) before the expensive Noise `ReadMessage` step is performed for unauthenticated peers.
- **Long term:** Document the intended trust/cost boundary for pre-authentication packet handling, add negative/load tests that simulate handshake floods from unauthenticated senders, and consider capping total concurrent pending (unauthenticated) handshake machines system-wide, not just per-VPN-IP after establishment.

### Proof of Concept
1. Craft a UDP packet with a valid Nebula header: `Version=header.Version`, `Type=header.Handshake`, `Subtype=header.HandshakeIXPSK0`, `RemoteIndex=0`, `MessageCounter=1`, followed by an arbitrary (or replayed) Noise stage-1 body.
2. Send a high-rate stream of such packets (optionally with randomized/spoofed source `AddrPort`s) to a target Nebula node's UDP listen port — no CA cert or prior tunnel is required.
3. Each packet reaches `outside.go:readOutsidePackets` → `HandshakeManager.HandleIncoming` → `beginHandshake` → `handshake.NewMachine(...).ProcessPacket`, incurring a Noise `ReadMessage` crypto operation per packet, as shown at [4](#0-3) .
4. Observe CPU saturation on the target node scaling with attacker packet rate, degrading or blocking legitimate handshake/traffic processing — no rate limiter or per-source cap exists to stop the flood.

### Citations

**File:** outside.go (L76-80)
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
