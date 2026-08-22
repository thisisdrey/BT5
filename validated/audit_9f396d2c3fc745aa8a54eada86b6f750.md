## Analysis

The bug class in the report is: an authentication/challenge-response step that performs non-trivial cryptographic work (challenge generation, signature verification) for **every** inbound request, with no per-source rate limiting, letting an unauthenticated attacker force unbounded CPU spend and DoS the service.

The nebula analog for this pattern is the handshake ingestion path, `HandshakeManager.HandleIncoming` / `beginHandshake`, which runs a full Noise IX handshake step (X25519 Diffie-Hellman + AEAD + certificate parsing/verification) for every UDP packet claiming to be a stage-1 handshake, before any peer authentication is established, and with no rate limiting per source address. [1](#0-0) 

Specifically:
- `HandleIncoming` only filters on `Subtype` and a static `RemoteAllowList` (a config-based allow/deny list, not a rate limiter), then dispatches any stage-1 packet (`MessageCounter == 1`, `RemoteIndex == 0`) straight to `beginHandshake`. [2](#0-1) 

- `beginHandshake` unconditionally constructs a new `handshake.Machine` and calls `machine.ProcessPacket`, which performs the Noise IX cryptographic exchange (DH computation, cert verification via `hm.certVerifier()`) for *every* such packet from *any* source — there is no counter, token bucket, or per-source throttle anywhere in this path. [3](#0-2) 

- The only allow-list check (`AllowUnknownVpnAddr`) is a static configuration match, not a dynamic rate limiter, and it is skipped entirely for relayed traffic (`via.IsRelayed`). [4](#0-3) 

This mirrors the reported issue almost exactly: a pre-authentication step with "non-negligible resource usage" (there, challenge issuance + signature verification; here, DH + cert verification) is reachable by anyone on the network with no rate limiting, enabling repeated invocation as a CPU-exhaustion / DoS vector against the responder.

### Title
Unrate-limited stage-1 handshake processing enables remote CPU-exhaustion DoS - (File: handshake_manager.go)

### Summary
Nebula's UDP listener routes every packet whose header claims to be a stage-1 (`MessageCounter == 1`, `RemoteIndex == 0`) Noise IX handshake message directly into `HandshakeManager.beginHandshake`, which performs a full Diffie-Hellman handshake step and certificate verification. No rate limiting, per-source counter, or backoff is applied before this expensive cryptographic work is performed, so any unauthenticated remote sender can force the target to repeatedly do full handshake crypto.

### Finding Description
`HandleIncoming` gates stage-1 packets only on message subtype and header fields, then calls `beginHandshake` for every packet that looks like a fresh handshake initiation. [5](#0-4) 
`beginHandshake` builds a new `handshake.Machine` and calls `machine.ProcessPacket`, performing the responder-side Noise IX cryptographic operations (DH exchange, PSK mixing, AEAD, and certificate parsing/verification via `hm.certVerifier()`) before any peer identity has been authenticated. [3](#0-2) 
The only gate ahead of this expensive work is a static config-driven allow list check (`AllowUnknownVpnAddr`), which is not a rate limiter and is skipped entirely for relayed traffic. [4](#0-3) 
There is no per-source-address counter, token bucket, or timeout/backoff logic anywhere in this ingestion path to throttle repeated stage-1 attempts from the same or spoofed source addresses, unlike the outbound handshake path which is rate-limited via `OutboundHandshakeTimer`/retry counters.

### Impact Explanation
An attacker with no valid certificate can send a stream of crafted stage-1 handshake packets (with spoofed or arbitrary source addresses) at line rate. Each packet forces the responder to perform DH computation and certificate parsing before any validity check fails, consuming CPU proportional to attacker-controlled packet rate. This is a remote, pre-authentication resource-exhaustion vector matching a Denial-of-Service impact.

### Likelihood Explanation
Any host that can reach the nebula UDP listen port can trigger this path; no valid certificate, prior handshake, or lighthouse registration is required. The only precondition is passing the (often permissive-by-default) remote allow list, which is a static filter, not a countermeasure against volume/rate abuse.

### Recommendation
Add origin-based rate limiting (e.g., a token bucket or basic per-source-address counter with backoff) in `HandleIncoming` before dispatching to `beginHandshake`, incrementing the limiter even on failed/invalid handshake attempts, and consider a lightweight pre-check (e.g., minimal packet-shape/proof-of-work check) prior to running the full Noise IX exchange, matching the report's recommendation to rate limit "immediately at the attempt of the call" and score/timeout repeatedly-failing sources.

### Proof of Concept
An attacker sends an arbitrary sequence of UDP packets to the nebula listener with `header.H{Type: Handshake, Subtype: HandshakeIXPSK0, MessageCounter: 1, RemoteIndex: 0}` and a garbage or replayed Noise IX payload from many source ports/spoofed addresses. Each packet reaches `beginHandshake` and triggers a full DH + cert-parse cycle in `machine.ProcessPacket` regardless of validity, with no rate limit rejecting the excess volume. [6](#0-5)

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
