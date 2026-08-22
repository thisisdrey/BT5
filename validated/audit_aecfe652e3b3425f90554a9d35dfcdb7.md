## Title
`RecvError` handling trusts unauthenticated source-address equality, allowing continuous spoofed tunnel teardown - ([File: outside.go])

### Summary
`handleRecvError` in `outside.go` decides whether to tear down a tunnel based solely on an equality check between the packet's source UDP address and the hostinfo's currently known remote address (`hr != addr`). `RecvError` packets are processed in the fully unencrypted/unauthenticated branch of `readOutsidePackets`, before any Noise decryption, certificate verification, or replay-window check occurs. An attacker who can observe the wire index (transmitted in cleartext in every packet header) and who can send a UDP packet with a spoofed source address matching a victim's known remote endpoint can repeatedly trigger `closeTunnel`, tearing down the tunnel over and over — a continuous denial-of-service on tunnel availability, analogous to the reported `payFastLane` DoS pattern where a strict equality check on attacker-influenceable state is used as the sole gate for a security-relevant action.

### Finding Description
In `outside.go`, `RecvError` is dispatched before any authentication: [1](#0-0) 

`handleRecvError` then performs its only "authentication" via a plain equality comparison of the observed source address against the hostinfo's currently cached remote: [2](#0-1) 

There is no cryptographic binding (no MAC, no encryption, no certificate check) tying the `RecvError` packet to the peer it claims to originate from — only:
1. `f.hostMap.QueryReverseIndex(h.RemoteIndex)` — a lookup keyed on a 32-bit index that is transmitted in cleartext on every wire packet (visible to any on-path/network observer), and
2. `hr != addr` — a strict equality check on the UDP source `netip.AddrPort`, which is trivially forgeable for UDP.

This mirrors the report's bug class: a strict equality check (`msg.value == balance`) is used as a security gate for a sensitive state transition, but the compared value can be manipulated by an attacker through a channel outside the intended, protected path (`selfdestruct` for ETH balance; UDP source-address spoofing plus index sniffing for `RecvError`). Once the equality is satisfied by the attacker-controlled input, `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` execute unconditionally, tearing down the session — and can be repeated indefinitely to keep the victim's tunnel from ever staying established, i.e., continuously blocking legitimate use, just as the report describes `payFastLane` being continuously blocked.

The gate `f.acceptRecvErrorConfig.ShouldRecvError(addr)` limits how often a given address triggers this path, but it is a rate limiter, not an authentication mechanism — it does not validate that the RecvError sender is cryptographically the peer, so an attacker within the allowed rate can still repeatedly force teardown once per allowed interval, which is sufficient to prevent stable tunnel operation.

### Impact Explanation
An attacker with only network visibility of the UDP path between two Nebula hosts (no CA-signed certificate, no valid handshake) can:
- Learn the wire index (`RemoteIndex`) from observed cleartext headers, and
- Spoof a UDP packet whose source address matches the victim's recorded remote, causing `handleRecvError` to pass its equality check and tear down the tunnel.

Repeating this yields a persistent denial of service against a specific tunnel/host pair, preventing the tunnel from remaining established (remote state poisoning / forced disconnection), which matches the "concrete... remote state poisoning" impact class called out in the validation criteria.

### Likelihood Explanation
Exploitation requires the attacker to both observe the index (visible in cleartext on any packet, so any on-path or reflective observer can obtain it) and spoof a UDP source address matching the victim's remote endpoint. UDP source-address spoofing is straightforward unless egress/ingress filtering (BCP38) is enforced by the network; for on-path or same-broadcast-domain attackers (a scenario explicitly reachable "with no CA-signed certificate," matching the rules), likelihood is moderate-to-high. The rate limiter (`ShouldRecvError`) reduces the tempo of the attack but does not prevent it, so repeated abuse over time remains feasible, echoing the "front-run and repeat" pattern of the original report.

### Recommendation
Do not treat UDP source-address equality as authentication for `RecvError`. Either:
- Require `RecvError` handling to be gated by a value derived from the authenticated session (e.g., only accept it after validating a MAC/signature computed with the session's Noise keys), or
- At minimum, cross-check against the replay/message-counter state and require corroborating evidence (e.g., only honor `RecvError` for indexes tied to very recent legitimate traffic, and require multiple additional non-spoofable signals) rather than a bare source-address string match.

### Proof of Concept
1. Attacker observes a `Message`/`Handshake` packet traveling between host A and host B (e.g., via ARP spoofing on a shared L2 segment, or any position with visibility into the UDP flow) and records the visible `RemoteIndex` used in the packet header.
2. Attacker crafts a `header.RecvError` packet with that `RemoteIndex` and sends it via UDP to host A with a spoofed source address equal to host B's known `netip.AddrPort`.
3. `readOutsidePackets` routes it directly to `handleRecvError` without decryption: [3](#0-2) 
4. `handleRecvError` finds the hostinfo via `QueryReverseIndex`, compares `hr != addr`, finds them equal (since the attacker spoofed the source to match), and calls `f.closeTunnel(hostinfo)`: [4](#0-3) 
5. Host A's tunnel to host B is torn down. Repeating this at the interval permitted by `ShouldRecvError` prevents the tunnel from ever remaining stable, denying service.

**Uncertainty note:** I was unable to fully inspect `interface.go`'s `sendRecvErrorConfig`/`acceptRecvErrorConfig` rate-limiter implementation details (exact throttling window/scope) within the available index; this affects only the *frequency* of exploitation, not the fundamental lack of cryptographic authentication in `handleRecvError`'s equality check. A full review of `interface.go` would be needed to precisely quantify the achievable attack rate.

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
