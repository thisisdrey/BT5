### Title
Unauthenticated `RecvError` packet accepted without verifying it corresponds to a self-initiated send, allowing remote tunnel-teardown (state poisoning/DoS) - (File: outside.go)

### Summary
`handleRecvError` processes a cleartext, unencrypted `RecvError` packet type before any certificate-based authentication takes place, and tears down an established tunnel based only on an index lookup and a source-address comparison, without any proof that the peer is reacting to a packet we actually sent.

### Finding Description
Nebula routes inbound UDP packets by header type before decryption. `header.RecvError` is dispatched straight out of `readOutsidePackets` with no certificate check at all: [1](#0-0) 

The handler `handleRecvError` then:
1. Gates on a config toggle (`acceptRecvErrorConfig`), which defaults to `always`.
2. Looks up the hostinfo by the cleartext `h.RemoteIndex` field carried in every packet header.
3. Compares the packet's source `netip.AddrPort` to the value cached in `hostinfo.GetRemote()`.
4. If both checks pass, immediately calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`. [2](#0-1) 

The default config values make this reachable by default: [3](#0-2) 

This mirrors the reported bug class: a message-processing callback that only checks "does this come from a plausible/allowed source" (here: index-exists + source-address match) but never checks "did *I* actually send something that would legitimately provoke this response." In the flash-loan report, the fix was to gate the callback on a `performingFlashLoan` flag set only around the contract's own outbound call. Nebula has no equivalent "I just sent an encrypted packet to this index/address and am expecting feedback" gate — any packet with the `RecvError` type, the correct (observable in cleartext on every packet) `RemoteIndex`, and a spoofed UDP source address matching the cached remote is accepted and acted upon.

Because `RemoteIndex` is transmitted in the clear on every Nebula packet (it is not itself secret — it's the routing index, not a session secret) and UDP source addresses are trivially spoofable to an off-path attacker who can determine (e.g., via traffic observation, since nebula runs over UDP with no source-port randomization defenses here) the victim's current negotiated remote endpoint and index, an attacker with no CA-signed certificate can forge a `RecvError` packet that causes the receiving node to tear down an active, authenticated tunnel to a legitimate peer.

### Impact Explanation
This causes a legitimate, already-authenticated Nebula tunnel to be forcibly torn down (`closeTunnel` + `DeleteHostInfo`) by an attacker who never presented a valid certificate. This is a remote state-poisoning / denial-of-service impact: repeated forged `RecvError` packets can be used to continuously disrupt overlay connectivity between two victim nodes, forcing constant re-handshaking or preventing stable communication, without the attacker needing any cryptographic material.

### Likelihood Explanation
Exploitation requires the attacker to know (or guess/observe) the target's `RemoteIndex` (visible in cleartext in every packet header on the wire) and to spoof the source UDP address to match the value the victim has cached as the peer's remote endpoint (an on-path or IP-spoofing-capable off-path attacker satisfies this). This is realistic on networks where UDP source-IP spoofing is not filtered, and the feature is enabled by default (`accept_recv_error: always`), making it broadly reachable.

### Recommendation
Do not act on `RecvError` purely based on index + source-address comparison. Require some proof of legitimacy that ties the error to a packet this node actually transmitted, e.g.:
- Track a short-lived "recently sent" nonce/counter per hostinfo/index and only honor `RecvError` if it references that pending state (analogous to the flash-loan fix's `performingFlashLoan` self-initiation flag).
- Rate-limit and require multiple corroborating events (e.g., no legitimate traffic actually failing) before tearing down a tunnel.
- Consider requiring `RecvError` to be at least loosely authenticated (e.g., only accepted from an address that has a live outstanding "waiting for ack" state) rather than any UDP datagram matching type + index + address.

### Proof of Concept
1. Establish a normal tunnel between node A and node B (cert-authenticated).
2. Attacker observes an in-flight Nebula packet on the wire and records `RemoteIndex` for A's tunnel to B, plus B's current UDP endpoint that A has cached.
3. Attacker crafts a bare `header.RecvError` packet (`header.Encode(..., header.RecvError, 0, index, 0)`), spoofing the UDP source address to B's endpoint, and sends it to A.
4. A's `readOutsidePackets` dispatches directly to `handleRecvError` (no cert check), finds the hostinfo via `QueryReverseIndex`, sees the spoofed source matches the cached remote, and calls `closeTunnel`/`DeleteHostInfo`, killing the legitimate tunnel — reproducible with `f.acceptRecvErrorConfig` at its default (`always`).

**Note on verification confidence:** I could not fully confirm within this session whether Nebula's UDP listener has any additional network-layer defenses (e.g., strict socket binding, connected-UDP semantics per peer, or OS-level anti-spoofing) that might reduce the practical spoofability of the source address; this would need to be validated against the actual `udp` package listener implementation to fully assess exploitability limits.

### Citations

**File:** outside.go (L76-84)
```go
	switch h.Type {
	case header.Handshake:
		f.handshakeManager.HandleIncoming(via, packet, h)
		return

	case header.RecvError:
		f.handleRecvError(via.UdpAddr, h)
		return
	}
```

**File:** outside.go (L541-575)
```go
func (f *Interface) handleRecvError(addr netip.AddrPort, h *header.H) {
	if !f.acceptRecvErrorConfig.ShouldRecvError(addr) {
		f.l.Debug("Recv error received, ignoring",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
		return
	}

	if f.l.Enabled(context.Background(), slog.LevelDebug) {
		f.l.Debug("Recv error received",
			"index", h.RemoteIndex,
			"udpAddr", addr,
		)
	}

	hostinfo := f.hostMap.QueryReverseIndex(h.RemoteIndex)
	if hostinfo == nil {
		f.l.Debug("Did not find remote index in main hostmap", "remoteIndex", h.RemoteIndex)
		return
	}

	hr := hostinfo.GetRemote()
	if hr.IsValid() && hr != addr {
		f.l.Info("Someone spoofing recv_errors?",
			"addr", addr,
			"hostinfoRemote", hr,
		)
		return
	}

	f.closeTunnel(hostinfo)
	// We also delete it from pending hostmap to allow for fast reconnect.
	f.handshakeManager.DeleteHostInfo(hostinfo)
}
```

**File:** interface.go (L459-480)
```go
func (f *Interface) reloadAcceptRecvError(c *config.C) {
	if c.InitialLoad() || c.HasChanged("listen.accept_recv_error") {
		stringValue := c.GetString("listen.accept_recv_error", "always")

		switch stringValue {
		case "always":
			f.acceptRecvErrorConfig = recvErrorAlways
		case "never":
			f.acceptRecvErrorConfig = recvErrorNever
		case "private":
			f.acceptRecvErrorConfig = recvErrorPrivate
		default:
			if c.GetBool("listen.accept_recv_error", true) {
				f.acceptRecvErrorConfig = recvErrorAlways
			} else {
				f.acceptRecvErrorConfig = recvErrorNever
			}
		}

		f.l.Info("Loaded accept_recv_error config", "acceptRecvError", f.acceptRecvErrorConfig.String())
	}
}
```
