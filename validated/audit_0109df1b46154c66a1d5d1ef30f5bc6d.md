### Title
Unauthenticated `RecvError` packet lets a certificate-less attacker force-close a victim's tunnel - (File: outside.go)

### Summary
`RecvError` is handled as an unencrypted, pre-authentication packet type in `Interface.readOutsidePackets`. An attacker with no CA-signed certificate and no completed handshake can spoof a UDP packet with this header type toward a victim, causing the victim to tear down an active tunnel to a legitimate peer, mirroring the class of bug described in the external report: an unauthenticated/under-authorized trigger that anyone can invoke to force a harmful state transition (there it was "unpause"/reset root state; here it is "force tunnel teardown").

### Finding Description
In `outside.go`, incoming UDP packets are parsed and, for a few types, handled before any decryption or certificate check: [1](#0-0) 

`header.RecvError` is dispatched straight to `f.handleRecvError(via.UdpAddr, h)` with only the header parsed — no certificate, no handshake state, no cryptographic authentication is required to reach this code path: [2](#0-1) 

Inside `handleRecvError`:
1. It checks `f.acceptRecvErrorConfig.ShouldRecvError(addr)` — this is a local admission/rate policy, not a proof of identity or possession of a valid cert.
2. It looks up the hostinfo via `h.RemoteIndex`, a 32-bit index that is observable on the wire (sent in cleartext in every packet header) or brute-forceable.
3. It compares `addr` (the UDP source address of the incoming packet, fully attacker-controlled/spoofable) against `hr` (the hostinfo's currently known remote `netip.AddrPort`). Since UDP has no source-address authentication, an attacker who can spoof/forge the source IP:port of a legitimate peer, or who is on-path/NAT-adjacent to it, can make this comparison succeed.
4. On match, it unconditionally calls `f.closeTunnel(hostinfo)` and deletes the pending hostinfo, immediately tearing down the tunnel.

This is functionally analogous to the report's `triggerRoot`: a function reachable by an unauthenticated caller that repeatedly forces a state transition (tunnel teardown / re-handshake requirement) that a legitimately-authenticated party did not request, without requiring the attacker to hold any credential (a CA-signed certificate) that the rest of the protocol otherwise requires for every other state-changing packet type (`Message`, `Test`, `CloseTunnel`, `Control` all require a resolved `hostinfo.ConnectionState` and successful AEAD decryption).

### Impact Explanation
Repeated forged `RecvError` packets let a network-adjacent or spoofing-capable attacker with **no valid certificate** and **no completed handshake**:
- Force-close established tunnels between two legitimate peers on demand (remote state poisoning / denial of service), analogous to the report's "block reward claims" impact — here it blocks/interrupts legitimate traffic.
- Repeatedly retrigger this to prevent a tunnel from staying up, similarly to how `triggerRoot` could be called "over and over" to continually disrupt state, since nothing in `handleRecvError` rate-limits per attacker or requires proof that the sender is the genuine remote peer.

### Likelihood Explanation
Exploitability depends on the attacker's ability to spoof the victim's peer's UDP source address (or be positioned to observe/guess `h.RemoteIndex`, which travels in cleartext on every packet of that tunnel and is therefore easy to observe by an on-path or off-path packet-sniffing attacker). This is a realistic threat model for a UDP-based overlay network like Nebula, where NATs and public/observable UDP traffic mean addresses and indices are not secret. No CA-signed certificate or successful handshake is required to reach or exploit this code path, satisfying the "no CA-signed certificate" reachability constraint.

### Recommendation
- Require some proof of legitimacy before acting on a `RecvError`, e.g., accept it only if it correlates with an actual outbound packet very recently sent to that exact `AddrPort`/index tuple (a short-lived pending-send cache), rather than trusting the spoofable UDP source address alone.
- Rate-limit `RecvError` handling per source/tuple more aggressively and require multiple corroborating signals (e.g., combined with a subsequent failed data-plane exchange) before tearing down a tunnel.
- Consider requiring `RecvError` (and any other pre-authentication packet types) to include a token/nonce that was established during the still-valid handshake, so it cannot be forged by a certificate-less off-path attacker.

### Proof of Concept
1. Victim A and legitimate peer B complete a handshake and have an active tunnel; A knows B's `RemoteIndex` (visible in every plaintext header) and B's `netip.AddrPort`.
2. Attacker C (holding no CA-signed certificate, uninvolved in the handshake) crafts a UDP packet with `header.Type = RecvError` and `RemoteIndex` set to the index A uses for B, and spoofs the source address to match B's known `AddrPort` (or is positioned to send from an equivalent vantage point, e.g., NAT/on-path).
3. C sends this packet to A.
4. `readOutsidePackets` routes it to `handleRecvError` without any decryption/cert check; the `addr == hr` check passes; A calls `closeTunnel` and deletes the pending hostinfo for B, terminating the tunnel — achieved entirely by an attacker without a valid certificate or completed handshake.

Note: I was unable to fully verify the implementation details of `acceptRecvErrorConfig.ShouldRecvError` and `sendRecvErrorConfig.ShouldRecvError` in `interface.go` (whether they impose any authentication-equivalent check beyond rate limiting) due to running out of investigation budget — this should be confirmed before finalizing the severity assessment.

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
