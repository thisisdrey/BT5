Found it: `handleRecvError` in `outside.go` is the closest reachable analog. It is completely unauthenticated (dispatched straight from `readOutsidePackets` before any cert or window check) and it tears down a live, authenticated tunnel based purely on a 32‑bit index guess plus a same-source-address check that an attacker sitting behind the same NAT/source (or capable of spoofing UDP source) can satisfy.

### Title
Unauthenticated `RecvError` packets allow remote teardown of established tunnels without any CA-signed certificate - (File: outside.go)

### Summary
`RecvError` packets are handled before any handshake, certificate, or replay-window check. An attacker with no certificate at all can send a bare `RecvError` header with a guessed/observed `RemoteIndex` and the sender's UDP source address, causing the victim to unilaterally close (`f.closeTunnel`) an existing, fully-authenticated tunnel and drop its pending handshake state.

### Finding Description
`readOutsidePackets` parses the wire header and, for `header.RecvError`, calls `f.handleRecvError` immediately — before any handshake/cert exists and before the replay window is consulted: [1](#0-0) 

`handleRecvError` performs almost no authentication of the sender. It only checks (a) an `acceptRecvErrorConfig` policy (address/allow-list based, not cryptographic) and (b) that the claimed sender address equals the currently recorded remote for that index: [2](#0-1) 

If those two (non-cryptographic) checks pass, it unconditionally tears the tunnel down and deletes the pending handshake state: [3](#0-2) 

The only "proof" tied to this action is `h.RemoteIndex` (a 32-bit value that traverses the network in plaintext in every packet header and is trivially observable by anyone who can see traffic, e.g. on-path or same-NAT attacker) and the UDP source `netip.AddrPort`, which for many attackers (same NAT, off-path spoofing when the underlay permits it) is also either observable or forgeable. There is no signature, MAC, or nonce binding the `RecvError` packet to the peer's Noise/cert identity — it is exactly the kind of "we trust a mutable, attacker-observable/forgeable piece of state (index+addr) to authorize a state-changing action" pattern flagged in the referenced report, where a cheaply manipulable value is used to trigger an asymmetric, high-impact state change (there the price/reserve ratio, here the RemoteIndex→tunnel binding).

This is structurally analogous to the report's flip-and-reverse pattern: the attacker doesn't need a valid certificate at all (unlike the DeFi bug where the attacker at least needed capital) — they just need to observe or reconstruct one 32-bit field and the source AddrPort, then can repeatedly force victims to tear down and rebuild tunnels, at effectively zero cost, similar to how the report's attacker repeatedly manipulates then reverses pool state "as long as it is profitable."

### Impact Explanation
This yields a remote, unauthenticated tunnel-teardown / DoS primitive: an attacker can force repeated tunnel teardown and rehandshaking for any host whose `RemoteIndex` and current remote `AddrPort` they can observe (e.g., anyone able to sniff/monitor UDP traffic to/from the target, or on shared/NATed networks), without holding a certificate signed by the mesh CA. Repeated triggering degrades availability and can be used to keep tunnels perpetually re-handshaking, amplifying handshake-processing load network-wide (a resource-exhaustion vector), and it can be timed to disrupt legitimate traffic (state poisoning of the sender's authoritative "GetRemote()" address doesn't change, but the tunnel itself is destroyed and must be renegotiated).

### Likelihood Explanation
Requires an attacker who can (1) learn a victim's active `RemoteIndex` for a tunnel (observable from any single packet on that flow, since it's sent in the clear in every header) and (2) either be at the same source `AddrPort` the victim last saw, or be able to spoof it. Off-path spoofing of the exact source AddrPort is nontrivial over the internet but easy on a shared LAN/NAT or when the attacker can sniff a live flow (on-path attacker, compromised gateway, malicious Wi-Fi AP, etc.) — none of which requires possessing a CA-signed nebula certificate. This is a materially lower bar than a full handshake compromise, making likelihood moderate.

### Recommendation
Do not let an unauthenticated `RecvError` packet alone tear down an established, cryptographically authenticated tunnel. At minimum: rate-limit and require corroborating evidence (e.g., require the tunnel to also have failed to receive traffic for some liveness period, or require `RecvError` to be authenticated/MACed with a key derived from the existing session rather than trusted purely from address+index), and log/alert on repeated `RecvError`-triggered teardowns from a single source. Consider requiring `RecvError` acceptance to be opt-in per remote (already partly gated by `acceptRecvErrorConfig`) and defaulting it to a stricter posture, and prefer soft signals (mark suspect, wait for corroboration) over hard `closeTunnel` on receipt of a single spoofable packet.

### Proof of Concept
1. Establish a normal nebula tunnel between victim `A` and peer `B`.
2. Attacker (holding no certificate for the mesh CA) observes one packet on the flow (or is co-located on `A`'s LAN/NAT) and learns `A`'s current `RemoteIndex` for the tunnel with `B`, and `B`'s `AddrPort` as seen by `A`.
3. Attacker crafts a bare `header.RecvError` packet (`header.Encode(..., header.RecvError, 0, index, 0)`) and sends it to `A`, spoofing (or naturally sharing) `B`'s `AddrPort` as the UDP source.
4. `readOutsidePackets` dispatches directly to `f.handleRecvError` with no cert/replay check [4](#0-3) ; `handleRecvError` finds the hostinfo by `RemoteIndex`, matches the spoofed source against `hostinfo.GetRemote()`, and calls `f.closeTunnel(hostinfo)` plus `f.handshakeManager.DeleteHostInfo(hostinfo)` [5](#0-4) .
5. `A`'s tunnel to `B` is destroyed and must fully rehandshake, at the cost to the attacker of a single unauthenticated UDP packet with no certificate involvement whatsoever.

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
