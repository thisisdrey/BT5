## Analog Found

### Title
Unauthenticated `RecvError` packets allow any network attacker to tear down established tunnels by replaying cleartext header fields - (File: `outside.go`)

### Summary
Nebula processes `header.RecvError` packets in the unencrypted/pre-authentication dispatch path, before any Noise handshake state or certificate has been verified for the sender. The handler (`handleRecvError`) trusts two fields that are either attacker-controlled or trivially observable/spoofable — the packet's cleartext `RemoteIndex` and the UDP source `netip.AddrPort` — to locate a live, fully-authenticated tunnel and unilaterally tear it down via `f.closeTunnel(hostinfo)`. This mirrors the RealityCards bug class: a state-mutating operation that is reachable without any authentication/authorization check, letting an attacker who has no certificate at all poison the tunnel state of two legitimate, CA-verified peers.

### Finding Description
`readOutsidePackets` in `outside.go` dispatches `header.RecvError` before decryption and before any peer-verification step: [1](#0-0) 

`handleRecvError` then looks up the hostinfo purely from the plaintext `RemoteIndex` in the packet header, and gates on a same-address check against the currently known remote: [2](#0-1) 

The only checks performed are:
1. `f.acceptRecvErrorConfig.ShouldRecvError(addr)` — per the changelog this option "defaults to `always`" (`CHANGELOG.md:130`), i.e. by default every source is accepted with no allow-list restriction.
2. `hr != addr` — a comparison against the UDP source address of the packet, which is not authenticated by anything (no Noise session, no MAC, no cert) for this message type; UDP source addresses are spoofable, and both endpoints of a tunnel plus the `RemoteIndex` value are visible in cleartext on every packet header exchanged between the pair (`header.H` fields are not encrypted; only the payload is, per `f.messageMetrics.Rx(h.Type, h.Subtype, 1)` handling in the same function and the `Message`/`Handshake` split at lines 76-84).

If both conditions are satisfied, `f.closeTunnel(hostinfo)` is called along with `f.handshakeManager.DeleteHostInfo(hostinfo)`, immediately destroying the tunnel state for both legitimate, certificate-verified peers — with no signature, no CA-verified certificate, and no Noise-authenticated session required from the sender of the `RecvError` packet.

This is directly analogous to `collectRentUser` in the reference report: a lightweight, unauthenticated operation (`collectRentUser`/`RecvError`) that anyone can invoke using only externally-observable identifiers (a `user` address/`type(uint256).max` timestamp vs. a `RemoteIndex`/spoofed source `AddrPort`), which flips victim state (`isForeclosed`/tunnel torn down) that a subsequent step exploits (`newRental` takeover / re-handshake hijack window).

### Impact Explanation
An attacker capable of UDP source-address spoofing (a well-known, low-cost technique that doesn't require holding a certificate or being an authenticated Nebula peer) and who can observe the cleartext `RemoteIndex` and address pair on the wire (e.g., an on-path network observer, a malicious router, or anyone who can capture a single packet between the victims) can force-close any live tunnel between two legitimate Nebula nodes at will. This is a remote denial-of-service / state-poisoning primitive against the "handshake authentication" and "nonce/replay handling" surface explicitly in scope: it lets an unauthenticated party unilaterally invalidate a peer's authenticated session state, disrupting connectivity and creating a re-handshake race window that could be leveraged similarly to how the RCTreasury exploit chained forced-foreclosure into ownership takeover (here: forced tunnel teardown could be chained with lighthouse/address-trust races described elsewhere in this codebase, e.g. `TestWrongResponderHandshake`, to redirect the ensuing re-handshake).

### Likelihood Explanation
Likelihood is elevated because:
- The default configuration accepts `recv_error` from `always` (no allow-list restriction) per `CHANGELOG.md:130`.
- The only anti-spoofing check is a UDP source-address string compare, which does not stop off-path spoofing.
- `RemoteIndex` is a cleartext header field on every packet, so any attacker who has observed even one packet of the target flow (passive on-path capture, or being co-located on a shared network segment) obtains everything needed to craft the malicious packet.
- No certificate, Noise session key, or MAC is required to trigger the teardown.

### Recommendation
Do not allow the encryption/authentication-free `RecvError` code path to unilaterally tear down an established, Noise-authenticated tunnel based solely on cleartext index/address matching. At minimum:
- Require `RecvError` packets be authenticated (e.g., carry a MAC computed under the tunnel's established key) before acting on them, similar to how `CloseTunnel` teardown is only accepted when it arrives properly encrypted under the tunnel's `ConnectionState` (see `TestCloseTunnelAuthenticated` in `e2e/tunnels_test.go:471-573`, which verifies that a bogus, unauthenticated `CloseTunnel` packet is correctly rejected).
- Tighten `acceptRecvErrorConfig`'s default away from unconditionally accepting recv_error from any source, and/or rate-limit/backoff repeated recv_error-triggered teardowns per remote index to blunt blind/observed-header spoofing.

### Proof of Concept
1. Two Nebula peers A and B establish a tunnel; the attacker (holding no CA-issued certificate) is positioned to observe or capture a single packet of the resulting flow (e.g. via a shared network segment or a compromised on-path router), learning: (a) B's current `AddrPort`, and (b) the cleartext `RemoteIndex` value A uses for B's hostinfo (visible in every plaintext packet header per `header.H`, since only the AEAD payload — not the header — is encrypted).
2. The attacker crafts a `header.RecvError` packet: `header.Encode(buf, header.Version, header.RecvError, 0, <observed RemoteIndex>, 0)` and sends it via spoofed UDP source `= B's AddrPort` to A's listener.
3. `readOutsidePackets` routes it directly to `f.handleRecvError(via.UdpAddr, h)` at `outside.go:81-84`, bypassing all decryption/authentication.
4. `handleRecvError` finds A's hostinfo for B via `QueryReverseIndex(h.RemoteIndex)`, the spoofed `addr` matches `hr` (`hostinfo.GetRemote()`), and the check at `outside.go:564` passes.
5. `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)` execute — A's fully-authenticated tunnel to B is torn down by a party holding no certificate and no valid Noise session, purely from spoofed/observed cleartext fields.

### Citations

**File:** outside.go (L75-84)
```go
	// Unencrypted packets
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
