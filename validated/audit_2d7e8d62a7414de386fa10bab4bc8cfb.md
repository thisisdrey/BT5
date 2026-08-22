Based on the investigation, the strongest reachable analog in this codebase is unauthenticated flooding of handshake stage-1 messages, which forces expensive asymmetric Noise handshake processing (DH computations) on the responder before any certificate is verified, with no rate limiting or cost accounting to prevent an attacker from cheaply monopolizing this processing — mirroring the reported bug class where a single cheap unit of work can be crafted to occupy disproportionate resources unchecked by any pricing/limiting mechanism.

### Title
Unrate-limited handshake stage-1 processing allows unauthenticated CPU-exhaustion block-stuffing - (File: handshake_manager.go)

### Summary
`HandshakeManager.HandleIncoming` in `handshake_manager.go` accepts any inbound stage-1 (`h.MessageCounter == 1`) handshake packet from an unauthenticated sender and immediately calls `beginHandshake`, which constructs a new `handshake.Machine` and calls `ProcessPacket`, performing full Noise (`X25519`) Diffie-Hellman computation before any certificate is validated. [1](#0-0) 

There is no per-source rate limiting, connection-attempt cap, or cost accounting anywhere on this path — `grep` for rate limiting primitives in the whole repository returns no production hits.

### Finding Description
`beginHandshake` unconditionally builds a new `handshake.Machine` and invokes `machine.ProcessPacket(nil, packet)`, which performs the expensive Noise handshake read (asymmetric crypto operations) as soon as a well-formed stage-1 packet arrives, *before* certificate reconstruction/verification (`validateCert`) is even reached: [2](#0-1) 

The only gates before this expensive work are: subtype match, an IP-based `remote_allow_list` check, and a zero-`RemoteIndex` check on stage-1 packets — none of which bound the *rate* or *volume* of distinct, cheaply-crafted stage-1 packets an attacker can send: [3](#0-2) 

The cost asymmetry mirrors the reported bug class precisely: in `Rollup.sol`, a single cheap transaction (fits exactly one blob) is disproportionately expensive to the network because the system never accounts for or splits that cost across capacity units, letting an attacker "stuff" a block cheaply. Here, a single cheap, self-signed (fake key) Noise message triggers real elliptic-curve DH computation on the victim with no CA-signed certificate required and no limiting mechanism, letting an attacker "stuff" the responder's handshake-processing capacity with an arbitrary volume of forged stage-1 packets, each of which is nearly free to produce (just a valid ephemeral X25519 key and Noise framing) but costly to process.

### Impact Explanation
An attacker with no valid CA-signed certificate can flood a node with distinct stage-1 handshake packets (varying ephemeral keys to avoid any incidental noise-state caching), forcing the responder to spend CPU on Noise DH operations for each one. Because there is no rate limiting, connection cap, or proof-of-work/cost gate before this expensive operation, an attacker can consume responder CPU cheaply enough to delay or block legitimate handshake completion and other traffic — a remote, unauthenticated denial-of-service impacting availability of the mesh, analogous to the "block stuffing" impact (oracles going stale, liquidations delayed) described in the report, here manifesting as delayed/failed tunnel establishment for legitimate peers.

### Likelihood Explanation
Likelihood is high: reaching `beginHandshake` requires only sending a UDP packet with a valid header, a supported subtype (`HandshakeIXPSK0`), and `RemoteIndex == 0` — none of which require possession of a CA-signed certificate. The `remote_allow_list` only filters by source IP/subnet, which is trivially satisfied by any attacker inside the allowed range (or absent an allow list, by anyone). No additional precondition is required.

### Recommendation
Introduce rate limiting / resource accounting on the handshake responder path — e.g., per-source-IP or global limits on in-flight/pending `HandshakeHostInfo` entries and on the rate of stage-1 packets processed per second, and/or defer expensive Noise processing behind a cheap pre-check (e.g., stateless cookie/puzzle) so that unauthenticated senders cannot force disproportionate cryptographic work relative to their own cost, closing the same class of unaccounted-for cost/capacity mismatch identified in the `Rollup.sol` report.

### Proof of Concept
1. Attacker crafts arbitrarily many syntactically valid stage-1 `HandshakeIXPSK0` packets, each with a fresh ephemeral X25519 public key, `RemoteIndex = 0`, and `MessageCounter = 1`.
2. Attacker sends these packets to the victim node's UDP listener from an address permitted by `remote_allow_list` (or from many sources if none is configured).
3. Each packet reaches `HandshakeManager.HandleIncoming` → `beginHandshake` → `handshake.Machine.ProcessPacket`, which performs a full Noise `ReadMessage` (DH) before certificate validation fails and the attempt is discarded.
4. Repeating this at volume consumes responder CPU with no rate limit or cost check in place, degrading/blocking legitimate handshake processing.

### Citations

**File:** handshake_manager.go (L151-194)
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

	// Continuation message must match a pending handshake by index.
	// Anything else is an orphaned packet (e.g., late retransmit after
	// timeout) and is dropped.
	if hh := hm.queryIndex(h.RemoteIndex); hh != nil {
		hm.continueHandshake(via, hh, packet)
		return
	}
}
```

**File:** handshake_manager.go (L712-726)
```go
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
