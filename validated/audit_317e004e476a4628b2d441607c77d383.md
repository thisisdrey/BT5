### Title
Tunnel teardown via spoofed unauthenticated `RecvError` packet accepted on source-address match alone - ([File: outside.go])

### Summary
The Comet report describes `set_freeze_status` performing a privileged, disruptive state change (freezing the pool) without verifying that the caller is the authorized pool admin. The closest reachable analog in nebula is `Interface.handleRecvError` in `outside.go`, which tears down an established tunnel (a disruptive, connection-state-poisoning operation) based only on an unauthenticated, unencrypted `header.RecvError` packet whose only "access control" is a spoofable UDP source-address comparison — not a cryptographic proof of peer identity.

### Finding Description
`RecvError` is one of the two message types handled entirely before any certificate/handshake validation or AEAD decryption in `readOutsidePackets`: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely from the attacker-controlled `h.RemoteIndex` field in the cleartext header, and the only defense against forgery is comparing the packet's source `netip.AddrPort` to the hostinfo's currently known remote address: [2](#0-1) 

This check (`hr.IsValid() && hr != addr`) is not a cryptographic authentication of the sender — UDP source addresses are trivially spoofable by any attacker who is not behind strict egress/ingress filtering, and the `RemoteIndex` value is visible on the wire in normal handshake/data traffic to any passive observer positioned to see the victim's tunnel traffic. Unlike `header.CloseTunnel`, which is only actionable after successful AEAD decryption (i.e., only by a party already holding the negotiated session keys), `RecvError` is processed before decryption and requires no cert, no handshake completion, and no possession of any key material. An attacker with no CA-signed certificate at all can therefore forge this packet.

Once accepted, `handleRecvError` unconditionally calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, tearing down the victim's active tunnel state — a privileged, disruptive action analogous to `set_freeze_status` disrupting pool operation, gated by a check that is not equivalent to verifying the caller is the legitimate remote peer.

### Impact Explanation
A remote unauthenticated attacker who can spoof UDP packets from (or observe) the legitimate peer's `RemoteIndex`/current UDP endpoint can force repeated teardown of a victim's established Nebula tunnel, causing denial of service and repeated forced re-handshakes. This matches the "remote state poisoning" / "no-CA-cert-required disruption" pattern the report's bug class targets (an operation with real effects reachable without proper authorization of the caller).

### Likelihood Explanation
Exploitation requires either off-path UDP source-address spoofing (feasible on networks without proper anti-spoofing/BCP38 filtering, common on much of the public internet since Nebula runs over UDP with no additional session cookie for this control message) or observing the legitimate peer's traffic to learn `RemoteIndex` and current endpoint. No valid certificate, no participation in the Noise handshake, and no possession of any Nebula key material is required — only guessing/observing metadata and spoofing a UDP packet, so this is reachable by exactly the class of attacker the rules require ("no CA-signed certificate").

### Recommendation
Do not allow `RecvError` alone (with only a source-address match) to tear down tunnel state. At minimum, require the response to be rate-limited and treated as a hint to double-check liveness (e.g., trigger a `Test`/`TestRequest` probe over the existing encrypted session) rather than an immediate, unauthenticated `closeTunnel` + `DeleteHostInfo`. Any action with the disruptive effect of tearing down a tunnel should be gated behind proof of possession of the session's cryptographic material (as `CloseTunnel` already is, since it is processed post-decryption), not solely an IP/port comparison.

### Proof of Concept
1. Attacker observes (or knows via lighthouse/static config) the current UDP endpoint and `RemoteIndex` associated with a victim's live Nebula tunnel to peer B (both are visible in unencrypted header fields of ordinary traffic between the victim and B).
2. Attacker crafts a raw UDP packet with `header.H{Type: header.RecvError, RemoteIndex: <observed index>}`, encoded via `header.Encode`, and spoofs the source address to match B's current UDP endpoint as seen by the victim (or sends from an actual position where spoofing succeeds).
3. Victim's `readOutsidePackets` routes this to `handleRecvError` before any decryption/certificate check; the `addr == hr` check passes because the source address matches, and the victim's tunnel to B is torn down via `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`.
4. This can be repeated to persistently deny the victim's connectivity to B.

Note: I could not fully verify from indexed content whether `f.acceptRecvErrorConfig.ShouldRecvError(addr)` (an additional gate visible in `outside.go` line 542 and referenced 16 times in `interface.go`) provides any further authentication beyond rate-limiting/allow-listing by address, since the full definition of `RecvErrorConfig`/`ShouldRecvError` was not returned by search. If that config performs meaningful cryptographic or allow-list-based authentication beyond simple address/rate control, it could reduce (but likely not eliminate, given spoofability) the severity of this finding. A Devin session with full file access would be needed to confirm the exact semantics of `ShouldRecvError`.

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

**File:** outside.go (L541-574)
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
```
