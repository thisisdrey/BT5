### Title
Unauthenticated `RecvError` packets allow remote tunnel teardown / message-delivery DoS - ([File: outside.go])

### Summary
`Interface.handleRecvError` in `outside.go` tears down an active, fully-established tunnel (`f.closeTunnel(hostinfo)` + `f.handshakeManager.DeleteHostInfo(hostinfo)`) in response to a `header.RecvError` packet. This packet type carries no AEAD authentication tag — it is a bare header (`header.Encode(make([]byte, header.Len), header.Version, header.RecvError, 0, index, 0)`), so its `RemoteIndex` field and its UDP source address are both attacker-controllable/spoofable. The only defenses are a config gate (`accept_recv_error`, default `"always"`) and a soft address check that is bypassable when the target's remote address hasn't been observed yet or can be spoofed over UDP. [1](#0-0) 

### Finding Description
`readOutsidePackets` dispatches `header.RecvError` packets straight to `f.handleRecvError(via.UdpAddr, h)` before any decryption or authentication step — these are one of only two message types processed entirely outside the AEAD/replay-window path (the other being `header.Handshake`): [2](#0-1) 

`handleRecvError` then:
1. Checks a config toggle `acceptRecvErrorConfig` (default `"always"`, i.e. accept from any address unless administrator restricts it).
2. Looks up a live `HostInfo` purely from the plaintext `h.RemoteIndex` field via `f.hostMap.QueryReverseIndex(h.RemoteIndex)`.
3. Performs a weak sanity check: rejects only if the hostinfo already has a *valid* recorded remote address that differs from the packet's source address (`hr.IsValid() && hr != addr`).
4. If the check passes, it calls `f.closeTunnel(hostinfo)` and `f.handshakeManager.DeleteHostInfo(hostinfo)`, unconditionally destroying the tunnel state. [1](#0-0) 

This mirrors the reported bug class: an externally reachable function mutates authoritative protocol state (here, tearing down/marking a session as dead) based on attacker-supplied identifiers (`RemoteIndex`) without a cryptographic authenticity check tying the packet to the real peer. Just as the `ProtocolFeeHook.postDispatch()` bug let an attacker pre-mark a `messageId` to block the legitimate message, an attacker here can pre-emptively invalidate a `RemoteIndex`/tunnel entry to block legitimate message delivery between two Nebula nodes.

Because `RemoteIndex` is a 32-bit value transmitted in cleartext on every packet of an established tunnel (visible to any on-path or off-path observer who can see even one packet, and brute-forceable at ~4 billion values otherwise), and because UDP allows straightforward source-address spoofing (Nebula listens on UDP), an attacker who has observed (or guessed) a victim's `RemoteIndex` can forge a `RecvError` packet with a spoofed source address matching the victim's current remote, and the victim will tear down the tunnel — even though the real remote peer never asked for teardown.

### Impact Explanation
Successful exploitation forcibly disestablishes a Nebula tunnel between two legitimate peers by injecting a single spoofed, unauthenticated UDP packet. Effects include:
- Denial of service: `f.closeTunnel(hostinfo)` and `hm.DeleteHostInfo(hostinfo)` drop `HostMap` and pending-handshake state for the peer, forcing an immediate re-handshake and dropping any in-flight data-plane traffic — a repeated flood of forged `RecvError` packets can keep tunnels perpetually torn down, defeating message delivery/connectivity (same "DoS of messages" impact class as the reported bug).
- If the attacker cannot yet spoof the source (`hr` already valid and mismatched), the attack is blocked by the address check, but this is not a cryptographic guarantee — it is a soft heuristic dependent on knowing/spoofing the correct source `netip.AddrPort` and on the hostinfo not yet having recorded a remote (e.g. for relayed peers, where `hostinfo.GetRemote()` may be invalid).

### Likelihood Explanation
- `RecvError` acceptance defaults to `"always"` (`f.acceptRecvErrorConfig` defaults to `recvErrorAlways`), so exploitation requires no special peer configuration.
- `RemoteIndex` is unencrypted in every packet header, so any attacker positioned to observe a single packet (or who is a legitimate peer on the network, per relay/lighthouse topology) recovers it trivially; brute force is also feasible given the small 32-bit space and no rate limiting mentioned for `RecvError`.
- UDP source-address spoofing is a well-known feasible technique, especially on networks without egress/ingress filtering.
- The check `hr.IsValid() && hr != addr` provides only partial mitigation, not authentication.

### Recommendation
Do not act on `RecvError` (or any state-mutating unauthenticated packet) using only cleartext identifiers and a soft address comparison. Recommended changes to `handleRecvError`/`sendRecvError` in `outside.go`:
- Require `RecvError` handling to only apply when the address check strictly matches a known/valid current remote (fail closed instead of allowing action when `hr` is invalid).
- Prefer authenticating recv-error notifications with the tunnel's AEAD key (e.g., wrap the notification inside the encrypted data channel, or include a MAC over `RemoteIndex` using session keys) rather than trusting a bare header field.
- Add rate limiting / dampening for `RecvError`-triggered teardown to blunt spoofing/flood attempts, and consider defaulting `accept_recv_error` to `"private"` or `"never"` rather than `"always"`.

### Proof of Concept
1. Observe (or brute-force) the victim's current `RemoteIndex` for a live tunnel, obtainable from any packet exchanged on that tunnel since it is sent in cleartext in the Nebula header.
2. Craft a raw UDP packet using `header.Encode(buf, header.Version, header.RecvError, 0, index, 0)` with `index` set to the victim's `RemoteIndex`.
3. Spoof the UDP source address to match the victim's currently-recorded remote address for that hostinfo (or send before the victim has recorded any remote/`hr.IsValid()==false`, e.g., during relay setup).
4. Send the packet to the victim's Nebula UDP listener; `handleRecvError` (outside.go:541) will locate the hostinfo via `QueryReverseIndex`, pass the address check, and call `closeTunnel`/`DeleteHostInfo`, tearing down the legitimate tunnel and disrupting message delivery until a new handshake completes.

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
